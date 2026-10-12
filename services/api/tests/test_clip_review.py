"""Tests for review decisions (CLIP-045)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.clip_candidates import (
    ClipCandidateSource,
    ClipCandidateStatus,
    ClipCandidateStore,
)
from clipos_api.clip_review import (
    CandidateDecision,
    ClipReviewStore,
    DecisionKind,
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
    path = Path("/tmp") / "clipos-045-clip-review-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ClipReviewStore:
    return ClipReviewStore.from_engine(engine)


def _register_candidate(engine: Engine) -> str:
    candidate_store = ClipCandidateStore.from_engine(engine)
    record = candidate_store.register_candidate(
        media_asset_id="asset-1",
        org_id="org-1",
        start_ms=1000,
        end_ms=10000,
        source=ClipCandidateSource.SEMANTIC.value,
        reason="fixture",
    )
    return record.candidate_id


def _decision(
    candidate_id: str, decision: DecisionKind = DecisionKind.APPROVE, actor: str = "reviewer-1"
) -> CandidateDecision:
    return CandidateDecision(
        candidate_id=candidate_id, org_id="org-1", decision=decision, actor_ref=actor
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_record_approve_transitions_status(
    store: ClipReviewStore, engine: Engine, audit_log: Path
) -> None:
    candidate_id = _register_candidate(engine)
    recorded = store.record_decision(_decision(candidate_id))
    assert recorded.decision is DecisionKind.APPROVE
    candidate = ClipCandidateStore.from_engine(engine).get(candidate_id, "org-1")
    assert candidate is not None
    assert candidate.status == ClipCandidateStatus.APPROVED.value
    assert "clip_candidates.decided" in audit_log.read_text(encoding="utf-8")


def test_record_reject_transitions_status(store: ClipReviewStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    store.record_decision(_decision(candidate_id, DecisionKind.REJECT))
    candidate = ClipCandidateStore.from_engine(engine).get(candidate_id, "org-1")
    assert candidate is not None
    assert candidate.status == ClipCandidateStatus.REJECTED.value


def test_record_identical_decision_is_idempotent(store: ClipReviewStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    first = store.record_decision(_decision(candidate_id))
    second = store.record_decision(_decision(candidate_id))
    assert first.decision_id == second.decision_id
    assert len(store.list_decisions(candidate_id, "org-1")) == 1


def test_record_conflict_on_different_actor(store: ClipReviewStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    store.record_decision(_decision(candidate_id, actor="reviewer-1"))
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.record_decision(_decision(candidate_id, actor="reviewer-2"))


def test_record_missing_candidate_raises(store: ClipReviewStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.record_decision(_decision("missing"))


def test_decision_reversal_allowed_and_flips_status(store: ClipReviewStore, engine: Engine) -> None:
    # APPROVE then REJECT: different kinds are distinct append-only rows.
    candidate_id = _register_candidate(engine)
    store.record_decision(_decision(candidate_id, DecisionKind.APPROVE, "reviewer-1"))
    store.record_decision(_decision(candidate_id, DecisionKind.REJECT, "reviewer-1"))
    candidate = ClipCandidateStore.from_engine(engine).get(candidate_id, "org-1")
    assert candidate is not None
    assert candidate.status == ClipCandidateStatus.REJECTED.value
    assert len(store.list_decisions(candidate_id, "org-1")) == 2


def test_list_decisions_org_scoped(store: ClipReviewStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    store.record_decision(_decision(candidate_id))
    assert len(store.list_decisions(candidate_id, "org-1")) == 1
    assert store.list_decisions(candidate_id, "org-other") == []


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/clip-candidates/{candidate_id}/decisions" in paths


def test_router_meta() -> None:
    from clipos_api.clip_review import router

    assert router.tags == ["clip-review"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def test_route_record_and_list_roundtrip(client, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    response = client.post(
        f"/clip-candidates/{candidate_id}/decisions",
        json={"org_id": "org-1", "decision": "APPROVE", "actor_ref": "reviewer-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "APPROVE"
    assert body["actor_ref"] == "reviewer-1"

    listing = client.get(f"/clip-candidates/{candidate_id}/decisions", params={"org_id": "org-1"})
    assert listing.status_code == 200
    assert len(listing.json()) == 1


def test_route_status_transition_visible_in_candidate_view(client, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    client.post(
        f"/clip-candidates/{candidate_id}/decisions",
        json={"org_id": "org-1", "decision": "REJECT", "actor_ref": "reviewer-1"},
    )
    fetched = client.get(f"/clip-candidates/{candidate_id}", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["candidate_id"] == candidate_id
    assert body["status"] == "REJECTED"


def test_route_missing_candidate_maps_to_404(client) -> None:
    response = client.post(
        "/clip-candidates/missing/decisions",
        json={"org_id": "org-1", "decision": "APPROVE", "actor_ref": "reviewer-1"},
    )
    assert response.status_code == 404


def test_route_conflict_maps_to_409(client, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    payload = {"org_id": "org-1", "decision": "APPROVE", "actor_ref": "reviewer-1"}
    assert (
        client.post(f"/clip-candidates/{candidate_id}/decisions", json=payload).status_code == 200
    )
    # identical re-record: idempotent 200
    assert (
        client.post(f"/clip-candidates/{candidate_id}/decisions", json=payload).status_code == 200
    )
    payload["actor_ref"] = "reviewer-2"
    assert (
        client.post(f"/clip-candidates/{candidate_id}/decisions", json=payload).status_code == 409
    )


def test_route_list_view_carries_candidate_id_and_status(client, engine: Engine) -> None:
    _register_candidate(engine)
    listing = client.get("/clip-candidates", params={"org_id": "org-1"})
    assert listing.status_code == 200
    items = listing.json()
    assert len(items) == 1
    assert items[0]["candidate_id"]
    assert items[0]["status"] == "NEW"
