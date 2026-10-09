"""Tests for opportunities, scores, and decisions (CLIP-036)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.opportunities import (
    DecisionKind,
    Opportunity,
    OpportunityDecision,
    OpportunityScore,
    OpportunityStatus,
    OpportunityStore,
    apply_opportunity_policy,
    router,
    score_opportunity,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.opportunities import (  # noqa: F401
        OpportunityDecisionRecord,
        OpportunityRecord,
        OpportunityScoreRecord,
    )
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-036-opportunities-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> OpportunityStore:
    return OpportunityStore.from_engine(engine)


def _opportunity(
    opportunity_id: str = "opp-1", org_id: str = "org-1", **changes: object
) -> Opportunity:
    values = {
        "opportunity_id": opportunity_id,
        "org_id": org_id,
        "topic_id": "ai-video",
        "title": "Test Opportunity",
        "description": "A test opportunity",
        "status": OpportunityStatus.OPEN,
    }
    values.update(changes)
    return Opportunity.model_validate(values)


def _score(
    score_id: str = "score-1",
    opportunity_id: str = "opp-1",
    org_id: str = "org-1",
    **changes: object,
) -> OpportunityScore:
    values = {
        "score_id": score_id,
        "opportunity_id": opportunity_id,
        "org_id": org_id,
        "components": {"trend_momentum": 0.9, "freshness": 0.6, "fit": 0.7},
        "total": 75.0,
    }
    values.update(changes)
    return OpportunityScore.model_validate(values)


def _decision(
    decision_id: str = "dec-1",
    opportunity_id: str = "opp-1",
    org_id: str = "org-1",
    **changes: object,
) -> OpportunityDecision:
    values = {
        "decision_id": decision_id,
        "opportunity_id": opportunity_id,
        "org_id": org_id,
        "decision": DecisionKind.APPROVE,
        "actor_ref": "operator:orchestrator",
        "reason_ref": "reviews://dec-1",
    }
    values.update(changes)
    return OpportunityDecision.model_validate(values)


# ----------------------------------------------------------------------
# Scoring purity
# ----------------------------------------------------------------------


def test_score_opportunity_weighted_sum() -> None:
    total = score_opportunity({"a": 1.0, "b": 0.5}, {"a": 2.0, "b": 1.0})
    assert total == pytest.approx(100.0 * (2.0 * 1.0 + 1.0 * 0.5) / 3.0)


def test_score_opportunity_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="positive finite"):
        score_opportunity({"a": 0.5}, {"a": -1.0})
    with pytest.raises(ValueError, match="finite in 0..1"):
        score_opportunity({"a": 1.5}, {"a": 1.0})
    with pytest.raises(ValueError, match="must not be empty"):
        score_opportunity({"a": 0.5}, {})


def test_apply_opportunity_policy_pure() -> None:
    policy = {"approve_at": 80.0, "reject_at": 30.0}
    assert apply_opportunity_policy(85.0, policy) == "approve"
    assert apply_opportunity_policy(50.0, policy) == "review"
    assert apply_opportunity_policy(20.0, policy) == "reject"
    with pytest.raises(ValueError, match="approve_at and reject_at"):
        apply_opportunity_policy(50.0, {})
    with pytest.raises(ValueError, match="thresholds must satisfy"):
        apply_opportunity_policy(50.0, {"approve_at": 10.0, "reject_at": 90.0})


# ----------------------------------------------------------------------
# Opportunities
# ----------------------------------------------------------------------


def test_register_opportunity_idempotent(store: OpportunityStore) -> None:
    first = store.register_opportunity(_opportunity())
    again = store.register_opportunity(_opportunity())
    assert first == again
    assert first.created_at is not None


def test_register_opportunity_conflict_raises(store: OpportunityStore) -> None:
    store.register_opportunity(_opportunity())
    with pytest.raises(ValueError, match="different fields"):
        store.register_opportunity(_opportunity(title="Different Title"))


def test_get_opportunity_roundtrip(store: OpportunityStore) -> None:
    registered = store.register_opportunity(_opportunity())
    stored = store.get_opportunity("opp-1", "org-1")
    assert stored == registered
    assert store.get_opportunity("missing", "org-1") is None


def test_opportunities_org_isolated(store: OpportunityStore) -> None:
    store.register_opportunity(_opportunity("opp-1", "org-1"))
    store.register_opportunity(_opportunity("opp-1", "org-2", title="Other"))
    assert len(store.list_opportunities("org-1")) == 1
    assert len(store.list_opportunities("org-2")) == 1


def test_list_opportunities_filters_status(store: OpportunityStore) -> None:
    store.register_opportunity(_opportunity("opp-1"))
    store.register_opportunity(_opportunity("opp-2", status=OpportunityStatus.EXPIRED))
    open_only = store.list_opportunities("org-1", OpportunityStatus.OPEN)
    assert [o.opportunity_id for o in open_only] == ["opp-1"]


# ----------------------------------------------------------------------
# Scores
# ----------------------------------------------------------------------


def test_score_requires_existing_opportunity(store: OpportunityStore) -> None:
    with pytest.raises(ValueError, match="opportunity not found"):
        store.record_score(_score())


def test_score_idempotent_and_status_bump(store: OpportunityStore) -> None:
    store.register_opportunity(_opportunity())
    first = store.record_score(_score(), OpportunityStatus.SCORED)
    again = store.record_score(_score(), OpportunityStatus.SCORED)
    assert first == again
    assert store.get_opportunity("opp-1", "org-1").status is OpportunityStatus.SCORED


def test_score_conflict_raises(store: OpportunityStore) -> None:
    store.register_opportunity(_opportunity())
    store.record_score(_score())
    with pytest.raises(ValueError, match="different fields"):
        store.record_score(_score(total=10.0, components={"a": 0.1}))


def test_get_score_roundtrip(store: OpportunityStore) -> None:
    store.register_opportunity(_opportunity())
    recorded = store.record_score(_score())
    assert store.get_score("opp-1") == recorded
    assert store.get_score("missing") is None


# ----------------------------------------------------------------------
# Decisions
# ----------------------------------------------------------------------


def test_decision_requires_existing_opportunity(store: OpportunityStore) -> None:
    with pytest.raises(ValueError, match="opportunity not found"):
        store.record_decision(_decision())


def test_decision_idempotent_and_status(store: OpportunityStore) -> None:
    store.register_opportunity(_opportunity())
    first = store.record_decision(_decision())
    again = store.record_decision(_decision())
    assert first == again
    assert store.get_opportunity("opp-1", "org-1").status is OpportunityStatus.APPROVED


def test_reject_decision_sets_rejected(store: OpportunityStore) -> None:
    store.register_opportunity(_opportunity())
    store.record_decision(_decision(decision=DecisionKind.REJECT))
    assert store.get_opportunity("opp-1", "org-1").status is OpportunityStatus.REJECTED


def test_decisions_append_many_ordered(store: OpportunityStore) -> None:
    store.register_opportunity(_opportunity())
    store.record_decision(_decision("dec-1"))
    store.record_decision(_decision("dec-2", decision=DecisionKind.REJECT))
    listed = store.list_decisions("org-1")
    assert [d.decision_id for d in listed] == ["dec-1", "dec-2"]


# ----------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------


def test_store_writes_audit_events(store: OpportunityStore) -> None:
    store.register_opportunity(_opportunity())
    store.record_score(_score())
    store.record_decision(_decision())
    events = audit.read_events()
    actions = [e["action"] for e in events]
    assert "opportunities.opportunity_registered" in actions
    assert "opportunities.score_recorded" in actions
    assert "opportunities.decision_recorded" in actions


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


def test_route_register_conflict_maps_to_409(store: OpportunityStore) -> None:
    from clipos_api.opportunities import register_opportunity_route

    store.register_opportunity(_opportunity())
    with pytest.raises(HTTPException) as err:
        register_opportunity_route(_opportunity(description=None), store)
    assert err.value.status_code == 409


def test_route_get_missing_maps_to_404(store: OpportunityStore) -> None:
    from clipos_api.opportunities import get_opportunity_route

    with pytest.raises(HTTPException) as err:
        get_opportunity_route("missing", store, "org-1")
    assert err.value.status_code == 404


def test_route_score_path_mismatch_maps_to_422(store: OpportunityStore) -> None:
    from clipos_api.opportunities import record_opportunity_score_route

    store.register_opportunity(_opportunity())
    with pytest.raises(HTTPException) as err:
        record_opportunity_score_route(_score(opportunity_id="other"), "opp-1", store)
    assert err.value.status_code == 422


def test_route_decision_not_found_maps_to_404(store: OpportunityStore) -> None:
    from clipos_api.opportunities import record_opportunity_decision_route

    with pytest.raises(HTTPException) as err:
        record_opportunity_decision_route(_decision(), "opp-1", store)
    assert err.value.status_code == 404


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/opportunities" in paths
    assert "/opportunities/{opportunity_id}" in paths
    assert "/opportunities/{opportunity_id}/scores" in paths
    assert "/opportunities/{opportunity_id}/decisions" in paths


def test_router_meta() -> None:
    assert router.tags == ["opportunities"]
