"""Tests for Agent Architect proposal generator (CLIP-083)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.architect import (
    GenerateEvidenceItem,
    GenerateProposalRequest,
    derive_evidence_id,
    derive_proposal_id,
    generate_proposal,
)
from clipos_api.improvement import ImprovementStore, ProposalStatus
from clipos_api.main import app


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-083-architect-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> ImprovementStore:
    return ImprovementStore.from_engine(engine)


def _request(**overrides: object) -> GenerateProposalRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "weakness_ref": "weakness://low-ctr-hooks",
        "title": "Improve early hooks",
        "summary": "CTR drops in first 2s.",
        "evidence": [
            GenerateEvidenceItem(
                evidence_ref="eval://hook-baseline",
                evidence_kind="eval-result",
            ),
            GenerateEvidenceItem(
                evidence_ref="signal://ctr-drop-week40",
                evidence_kind="signal",
                note_ref="note://week-40",
            ),
        ],
    }
    payload.update(overrides)
    return GenerateProposalRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure identity derivation
# ---------------------------------------------------------------------------


def test_proposal_id_deterministic() -> None:
    first = derive_proposal_id("org-1", "weakness://x")
    second = derive_proposal_id("org-1", "weakness://x")
    assert first == second
    assert first.startswith("gen-")
    assert derive_proposal_id("org-1", "weakness://y") != first
    assert derive_proposal_id("org-2", "weakness://x") != first


def test_evidence_id_deterministic() -> None:
    a = derive_evidence_id("gen-abc", "eval://one")
    b = derive_evidence_id("gen-abc", "eval://one")
    assert a == b
    assert a != derive_evidence_id("gen-abc", "eval://two")


# ---------------------------------------------------------------------------
# Store flow
# ---------------------------------------------------------------------------


def test_generate_creates_proposal_and_evidence(
    store: ImprovementStore,
    audit_log: Path,
) -> None:
    result = generate_proposal(store, _request())
    assert result.proposal.proposal_id == result.proposal_id
    assert result.proposal.status is ProposalStatus.PROPOSED
    assert result.proposal.weakness_ref == "weakness://low-ctr-hooks"
    assert len(result.evidence) == 2
    assert result.evidence_created == 2
    assert result.evidence_replayed == 0
    log = audit_log.read_text(encoding="utf-8")
    assert "improvement_proposals.registered" in log
    assert "proposal_evidence.linked" in log
    assert "proposal_generation.generated" in log


def test_generate_replay_is_noop(
    store: ImprovementStore,
    audit_log: Path,
) -> None:
    first = generate_proposal(store, _request())
    second = generate_proposal(store, _request())
    assert second.proposal_id == first.proposal_id
    assert second.evidence_created == 0
    assert second.evidence_replayed == 2
    assert len(store.list_proposals("org-1")) == 1
    assert len(store.list_evidence("org-1")) == 2


def test_generate_partial_new_evidence(store: ImprovementStore) -> None:
    generate_proposal(store, _request())
    extended = _request(
        evidence=[
            GenerateEvidenceItem(
                evidence_ref="eval://hook-baseline",
                evidence_kind="eval-result",
            ),
            GenerateEvidenceItem(
                evidence_ref="eval://fresh-eval",
                evidence_kind="eval-result",
            ),
        ]
    )
    result = generate_proposal(store, extended)
    assert result.evidence_created == 1
    assert result.evidence_replayed == 1
    assert len(store.list_evidence("org-1")) == 3


def test_generate_conflict_on_reworded_weakness(
    store: ImprovementStore,
) -> None:
    generate_proposal(store, _request())
    with pytest.raises(ValueError, match="already registered with different fields"):
        generate_proposal(store, _request(title="Different title"))


def test_generate_no_evidence(store: ImprovementStore) -> None:
    result = generate_proposal(store, _request(evidence=[]))
    assert result.evidence == []
    assert result.evidence_created == 0
    assert store.get_proposal(result.proposal_id, "org-1") is not None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/improvement-proposals/generate" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "weakness_ref": "weakness://low-ctr-hooks",
        "title": "Improve early hooks",
        "summary": "CTR drops in first 2s.",
        "evidence": [
            {"evidence_ref": "eval://hook-baseline", "evidence_kind": "eval-result"},
        ],
    }


def test_generate_route_roundtrip(client) -> None:
    created = client.post("/improvement-proposals/generate", json=_payload())
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["proposal_id"].startswith("gen-")
    assert body["evidence_created"] == 1
    assert body["proposal"]["status"] == "PROPOSED"

    # Fetch the generated proposal via the normal GET route.
    fetched = client.get(
        f"/improvement-proposals/{body['proposal_id']}", params={"org_id": "org-1"}
    )
    assert fetched.status_code == 200

    replay = client.post("/improvement-proposals/generate", json=_payload())
    assert replay.status_code == 200
    assert replay.json()["evidence_created"] == 0
    assert replay.json()["evidence_replayed"] == 1

    changed = client.post(
        "/improvement-proposals/generate",
        json={**_payload(), "title": "Reworded"},
    )
    assert changed.status_code == 409


def test_generate_bad_input_422(client) -> None:
    bad = client.post(
        "/improvement-proposals/generate",
        json={**_payload(), "evidence": [{"evidence_ref": "x", "evidence_kind": "bad/kind"}]},
    )
    assert bad.status_code == 422
