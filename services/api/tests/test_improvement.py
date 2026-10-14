"""Tests for improvement proposal evidence model (CLIP-082)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.improvement import (
    ImprovementStore,
    LinkEvidenceRequest,
    ProposalStatus,
    ProposalTransitionRequest,
    RegisterProposalRequest,
    proposal_transition_allowed,
)
from clipos_api.main import app


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-082-improvement-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> ImprovementStore:
    return ImprovementStore.from_engine(engine)


def _proposal(**overrides: object) -> RegisterProposalRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "proposal_id": "prop-1",
        "title": "Improve hook testing",
        "weakness_ref": "weakness://low-ctr",
        "summary": "Add early-hook A/B checks.",
    }
    payload.update(overrides)
    return RegisterProposalRequest(**payload)  # type: ignore[arg-type]


def _evidence(**overrides: object) -> LinkEvidenceRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "evidence_id": "ev-1",
        "proposal_id": "prop-1",
        "evidence_ref": "eval://hook-baseline",
        "evidence_kind": "eval-result",
        "note_ref": "note://week-40",
    }
    payload.update(overrides)
    return LinkEvidenceRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure lifecycle
# ---------------------------------------------------------------------------


def test_journey_to_verified() -> None:
    assert proposal_transition_allowed(ProposalStatus.PROPOSED, ProposalStatus.IN_REVIEW)
    assert proposal_transition_allowed(ProposalStatus.IN_REVIEW, ProposalStatus.ACCEPTED)
    assert proposal_transition_allowed(ProposalStatus.ACCEPTED, ProposalStatus.IMPLEMENTED)
    assert proposal_transition_allowed(ProposalStatus.IMPLEMENTED, ProposalStatus.VERIFIED)


def test_journey_to_rolled_back_and_rejected() -> None:
    assert proposal_transition_allowed(ProposalStatus.IMPLEMENTED, ProposalStatus.ROLLED_BACK)
    assert proposal_transition_allowed(ProposalStatus.IN_REVIEW, ProposalStatus.REJECTED)
    assert not proposal_transition_allowed(ProposalStatus.PROPOSED, ProposalStatus.ACCEPTED)
    assert not proposal_transition_allowed(ProposalStatus.VERIFIED, ProposalStatus.IMPLEMENTED)
    assert not proposal_transition_allowed(ProposalStatus.REJECTED, ProposalStatus.PROPOSED)
    assert proposal_transition_allowed(ProposalStatus.ACCEPTED, ProposalStatus.ACCEPTED)


# ---------------------------------------------------------------------------
# Store: proposals
# ---------------------------------------------------------------------------


def test_proposal_register_idempotent_conflict_audit(
    store: ImprovementStore,
    audit_log: Path,
) -> None:
    first = store.register_proposal(_proposal())
    again = store.register_proposal(_proposal())
    assert first.proposal_id == again.proposal_id
    assert first.status is ProposalStatus.PROPOSED
    assert "improvement_proposals.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_proposal(_proposal(title="Changed"))
    assert len(store.list_proposals("org-1")) == 1


def test_proposal_full_journey(store: ImprovementStore, audit_log: Path) -> None:
    store.register_proposal(_proposal())
    for status in (
        ProposalStatus.IN_REVIEW,
        ProposalStatus.ACCEPTED,
        ProposalStatus.IMPLEMENTED,
        ProposalStatus.VERIFIED,
    ):
        store.transition_proposal(
            "prop-1",
            "org-1",
            ProposalTransitionRequest(status=status, actor_ref="op"),
        )
    assert store.get_proposal("prop-1", "org-1").status is ProposalStatus.VERIFIED
    assert "improvement_proposals.status_changed" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="illegal proposal transition"):
        store.transition_proposal(
            "prop-1",
            "org-1",
            ProposalTransitionRequest(status=ProposalStatus.IMPLEMENTED, actor_ref="op"),
        )


def test_proposal_illegal_and_unknown(store: ImprovementStore) -> None:
    store.register_proposal(_proposal())
    with pytest.raises(ValueError, match="illegal proposal transition"):
        store.transition_proposal(
            "prop-1",
            "org-1",
            ProposalTransitionRequest(status=ProposalStatus.VERIFIED, actor_ref="op"),
        )
    with pytest.raises(ValueError, match="improvement proposal not found"):
        store.transition_proposal(
            "missing",
            "org-1",
            ProposalTransitionRequest(status=ProposalStatus.IN_REVIEW, actor_ref="op"),
        )


def test_list_proposal_status_filter(store: ImprovementStore) -> None:
    store.register_proposal(_proposal())
    store.register_proposal(_proposal(proposal_id="prop-2"))
    store.transition_proposal(
        "prop-2",
        "org-1",
        ProposalTransitionRequest(status=ProposalStatus.IN_REVIEW, actor_ref="op"),
    )
    assert len(store.list_proposals("org-1")) == 2
    assert len(store.list_proposals("org-1", ProposalStatus.PROPOSED)) == 1
    assert len(store.list_proposals("org-1", ProposalStatus.IN_REVIEW)) == 1
    assert len(store.list_proposals("org-other")) == 0


# ---------------------------------------------------------------------------
# Store: evidence
# ---------------------------------------------------------------------------


def test_evidence_requires_proposal(store: ImprovementStore) -> None:
    with pytest.raises(ValueError, match="improvement proposal not found"):
        store.link_evidence(_evidence())


def test_evidence_link_idempotent_conflict(
    store: ImprovementStore,
    audit_log: Path,
) -> None:
    store.register_proposal(_proposal())
    first = store.link_evidence(_evidence())
    again = store.link_evidence(_evidence())
    assert first.evidence_id == again.evidence_id
    assert "proposal_evidence.linked" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already linked with different fields"):
        store.link_evidence(_evidence(evidence_ref="eval://other"))
    assert len(store.list_evidence("org-1")) == 1


def test_evidence_list_proposal_filter(store: ImprovementStore) -> None:
    store.register_proposal(_proposal())
    store.register_proposal(_proposal(proposal_id="prop-2"))
    store.link_evidence(_evidence())
    store.link_evidence(_evidence(evidence_id="ev-2", proposal_id="prop-2"))
    assert len(store.list_evidence("org-1")) == 2
    assert len(store.list_evidence("org-1", "prop-1")) == 1
    assert len(store.list_evidence("org-1", "prop-2")) == 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/improvement-proposals" in paths
    assert "/improvement-proposals/{proposal_id}" in paths
    assert "/improvement-proposals/{proposal_id}/transition" in paths
    assert "/proposal-evidence" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_full_route_roundtrip(client) -> None:
    created = client.post(
        "/improvement-proposals",
        json={
            "org_id": "org-1",
            "proposal_id": "prop-1",
            "title": "Improve hook testing",
            "weakness_ref": "weakness://low-ctr",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "PROPOSED"

    fetched = client.get("/improvement-proposals/prop-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert client.get("/improvement-proposals/nope", params={"org_id": "org-1"}).status_code == 404

    conflict = client.post(
        "/improvement-proposals",
        json={
            "org_id": "org-1",
            "proposal_id": "prop-1",
            "title": "Changed",
            "weakness_ref": "weakness://low-ctr",
        },
    )
    assert conflict.status_code == 409

    for status in ("IN_REVIEW", "ACCEPTED", "IMPLEMENTED", "VERIFIED"):
        resp = client.post(
            "/improvement-proposals/prop-1/transition",
            params={"org_id": "org-1"},
            json={"status": status, "actor_ref": "op"},
        )
        assert resp.status_code == 200, (status, resp.text)

    evidence = client.post(
        "/proposal-evidence",
        json={
            "org_id": "org-1",
            "evidence_id": "ev-1",
            "proposal_id": "prop-1",
            "evidence_ref": "eval://hook-baseline",
            "evidence_kind": "eval-result",
        },
    )
    assert evidence.status_code == 200, evidence.text

    listing = client.get("/proposal-evidence", params={"org_id": "org-1", "proposal_id": "prop-1"})
    assert len(listing.json()) == 1

    proposals = client.get(
        "/improvement-proposals", params={"org_id": "org-1", "status": "VERIFIED"}
    )
    assert len(proposals.json()) == 1


def test_route_errors(client) -> None:
    orphan = client.post(
        "/proposal-evidence",
        json={
            "org_id": "org-1",
            "evidence_id": "ev-x",
            "proposal_id": "missing",
            "evidence_ref": "eval://x",
            "evidence_kind": "eval-result",
        },
    )
    assert orphan.status_code == 404

    missing_transition = client.post(
        "/improvement-proposals/nope/transition",
        params={"org_id": "org-1"},
        json={"status": "IN_REVIEW", "actor_ref": "op"},
    )
    assert missing_transition.status_code == 404

    bad_slug = client.post(
        "/improvement-proposals",
        json={
            "org_id": "org-1",
            "proposal_id": "bad/id",
            "title": "T",
            "weakness_ref": "w",
        },
    )
    assert bad_slug.status_code == 422
