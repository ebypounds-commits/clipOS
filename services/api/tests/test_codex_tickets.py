"""Tests for Codex engineering-ticket generation/export (CLIP-085)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.codex_tickets import (
    EngineeringTicketStore,
    GenerateTicketRequest,
    TicketStatus,
    TicketTransitionRequest,
    generate_packet_markdown,
    ticket_transition_allowed,
)
from clipos_api.improvement import (
    ImprovementStore,
    LinkEvidenceRequest,
    ProposalStatus,
    RegisterProposalRequest,
)
from clipos_api.main import app

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_packet", _REPO_ROOT / "scripts" / "validate_packet.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-085-tickets-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> EngineeringTicketStore:
    return EngineeringTicketStore.from_engine(engine)


def _seed_accepted_proposal(
    engine: Engine, status: ProposalStatus = ProposalStatus.ACCEPTED
) -> None:
    improvements = ImprovementStore.from_engine(engine)
    improvements.register_proposal(
        RegisterProposalRequest(
            org_id="org-1",
            proposal_id="prop-1",
            title="Improve hook testing",
            weakness_ref="weakness://low-ctr",
            summary="CTR drops in first 2s.",
        )
    )
    improvements.link_evidence(
        LinkEvidenceRequest(
            org_id="org-1",
            evidence_id="ev-1",
            proposal_id="prop-1",
            evidence_ref="eval://hook-baseline",
            evidence_kind="eval-result",
        )
    )
    path = {
        ProposalStatus.PROPOSED: [],
        ProposalStatus.IN_REVIEW: [ProposalStatus.IN_REVIEW],
        ProposalStatus.ACCEPTED: [ProposalStatus.IN_REVIEW, ProposalStatus.ACCEPTED],
        ProposalStatus.IMPLEMENTED: [
            ProposalStatus.IN_REVIEW,
            ProposalStatus.ACCEPTED,
            ProposalStatus.IMPLEMENTED,
        ],
    }[status]
    from clipos_api.improvement import ProposalTransitionRequest

    for step in path:
        improvements.transition_proposal(
            "prop-1",
            "org-1",
            ProposalTransitionRequest(status=step, actor_ref="op"),
        )


def _request(**overrides: object) -> GenerateTicketRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "ticket_id": "eng-1",
        "proposal_id": "prop-1",
        "orchestrator_run_id": "run-1",
        "branch": "agent/eng-1",
        "worktree": "/srv/clipos/worktrees/eng-1",
        "lease_expires_at": "2026-10-15T00:00:00+00:00",
    }
    payload.update(overrides)
    return GenerateTicketRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure packet renderer
# ---------------------------------------------------------------------------


def test_generated_packet_is_validator_clean(tmp_path: Path) -> None:
    validator = _load_validator()
    packet = generate_packet_markdown(
        ticket_id="eng-1",
        orchestrator_run_id="run-1",
        branch="agent/eng-1",
        worktree="/srv/clipos/worktrees/eng-1",
        lease_expires_at="2026-10-15T00:00:00+00:00",
        objective="Implement the accepted improvement for weakness://low-ctr.",
        weakness_ref="weakness://low-ctr",
        evidence_refs=["eval://baseline", "signal://ctr-drop"],
    )
    target = tmp_path / "packet.md"
    target.write_text(packet, encoding="utf-8")
    problems = validator.validate_packet(target)
    assert problems == []
    assert "# Codex Task Packet" in packet
    assert "ticket_id: eng-1" in packet
    assert "orchestrator_run_id: run-1" in packet
    assert "eval://baseline" in packet


def test_packet_tolerates_empty_evidence(tmp_path: Path) -> None:
    validator = _load_validator()
    packet = generate_packet_markdown(
        ticket_id="eng-2",
        orchestrator_run_id="run-2",
        branch="b",
        worktree="/w",
        lease_expires_at="",
        objective="Objective text",
        weakness_ref="weakness://x",
        evidence_refs=[],
    )
    target = tmp_path / "packet2.md"
    target.write_text(packet, encoding="utf-8")
    assert validator.validate_packet(target) == []
    assert "(no evidence links recorded)" in packet


def test_transition_table() -> None:
    assert ticket_transition_allowed(TicketStatus.GENERATED, TicketStatus.EXPORTED)
    assert ticket_transition_allowed(TicketStatus.EXPORTED, TicketStatus.IMPLEMENTED)
    assert not ticket_transition_allowed(TicketStatus.GENERATED, TicketStatus.IMPLEMENTED)
    assert not ticket_transition_allowed(TicketStatus.IMPLEMENTED, TicketStatus.EXPORTED)
    assert ticket_transition_allowed(TicketStatus.IMPLEMENTED, TicketStatus.IMPLEMENTED)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_generate_idempotent_conflict_audit(
    store: EngineeringTicketStore,
    engine: Engine,
    audit_log: Path,
) -> None:
    _seed_accepted_proposal(engine)
    first = store.generate(_request())
    assert first.status is TicketStatus.GENERATED
    assert "# Codex Task Packet" in first.packet_markdown
    assert "weakness://low-ctr" in first.packet_markdown
    assert "eval://hook-baseline" in first.packet_markdown
    assert "engineering_tickets.generated" in audit_log.read_text(encoding="utf-8")

    again = store.generate(_request())
    assert again.ticket_id == first.ticket_id
    assert len(store.list_tickets("org-1")) == 1
    with pytest.raises(ValueError, match="already generated with different fields"):
        store.generate(_request(branch="agent/other"))


def test_generate_requires_accepted_proposal(
    store: EngineeringTicketStore,
    engine: Engine,
) -> None:
    _seed_accepted_proposal(engine, status=ProposalStatus.PROPOSED)
    with pytest.raises(ValueError, match="proposal not ready"):
        store.generate(_request())
    with pytest.raises(ValueError, match="improvement proposal not found"):
        store.generate(_request(proposal_id="missing"))


def test_lifecycle_flow(
    store: EngineeringTicketStore,
    engine: Engine,
    audit_log: Path,
) -> None:
    _seed_accepted_proposal(engine)
    store.generate(_request())
    exported = store.transition(
        "eng-1",
        "org-1",
        TicketTransitionRequest(status=TicketStatus.EXPORTED, actor_ref="op"),
    )
    assert exported.status is TicketStatus.EXPORTED
    implemented = store.transition(
        "eng-1",
        "org-1",
        TicketTransitionRequest(
            status=TicketStatus.IMPLEMENTED, actor_ref="op", reason_ref="merged"
        ),
    )
    assert implemented.status is TicketStatus.IMPLEMENTED
    assert "engineering_tickets.status_changed" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="illegal ticket transition"):
        store.transition(
            "eng-1",
            "org-1",
            TicketTransitionRequest(status=TicketStatus.GENERATED, actor_ref="op"),
        )
    with pytest.raises(ValueError, match="engineering ticket not found"):
        store.transition(
            "missing",
            "org-1",
            TicketTransitionRequest(status=TicketStatus.EXPORTED, actor_ref="op"),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/engineering-tickets" in paths
    assert "/engineering-tickets/{ticket_id}" in paths
    assert "/engineering-tickets/{ticket_id}/export" in paths
    assert "/engineering-tickets/{ticket_id}/transition" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _proposal_for_routes(client) -> None:
    client.post(
        "/improvement-proposals",
        json={
            "org_id": "org-1",
            "proposal_id": "prop-1",
            "title": "Improve hooks",
            "weakness_ref": "weakness://low-ctr",
        },
    )
    for status in ("IN_REVIEW", "ACCEPTED"):
        client.post(
            "/improvement-proposals/prop-1/transition",
            params={"org_id": "org-1"},
            json={"status": status, "actor_ref": "op"},
        )


def _ticket_payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "ticket_id": "eng-1",
        "proposal_id": "prop-1",
        "orchestrator_run_id": "run-1",
        "branch": "agent/eng-1",
        "worktree": "/srv/clipos/worktrees/eng-1",
        "lease_expires_at": "2026-10-15T00:00:00+00:00",
    }


def test_full_route_roundtrip(client) -> None:
    _proposal_for_routes(client)
    created = client.post("/engineering-tickets", json=_ticket_payload())
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "GENERATED"
    assert body["packet_markdown"].startswith("# Codex Task Packet")

    fetched = client.get("/engineering-tickets/eng-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert client.get("/engineering-tickets/nope", params={"org_id": "org-1"}).status_code == 404

    listing = client.get("/engineering-tickets", params={"org_id": "org-1"})
    assert len(listing.json()) == 1

    exported = client.get("/engineering-tickets/eng-1/export", params={"org_id": "org-1"})
    assert exported.status_code == 200
    assert "text/markdown" in exported.headers.get("content-type", "")
    assert exported.text.startswith("# Codex Task Packet")
    assert "eng-1.md" in exported.headers.get("content-disposition", "")

    transitioned = client.post(
        "/engineering-tickets/eng-1/transition",
        params={"org_id": "org-1"},
        json={"status": "EXPORTED", "actor_ref": "op"},
    )
    assert transitioned.status_code == 200, transitioned.text
    assert transitioned.json()["status"] == "EXPORTED"

    conflict = client.post("/engineering-tickets", json={**_ticket_payload(), "branch": "other"})
    assert conflict.status_code == 409


def test_route_proposal_not_ready_409(client) -> None:
    client.post(
        "/improvement-proposals",
        json={
            "org_id": "org-1",
            "proposal_id": "prop-1",
            "title": "Improve hooks",
            "weakness_ref": "weakness://low-ctr",
        },
    )
    resp = client.post("/engineering-tickets", json=_ticket_payload())
    assert resp.status_code == 409
    assert "not ready" in resp.json()["detail"]


def test_route_bad_slug_422(client) -> None:
    payload = _ticket_payload()
    payload["ticket_id"] = "bad/id"
    resp = client.post("/engineering-tickets", json=payload)
    assert resp.status_code == 422
