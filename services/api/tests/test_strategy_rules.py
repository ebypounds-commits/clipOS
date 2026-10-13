"""Tests for strategy-rule canary/promotion/rollback (CLIP-069)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.strategy_rules import (
    RegisterRuleRequest,
    RuleStatus,
    RuleTransitionRequest,
    StrategyRuleStore,
    rule_transition_allowed,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-069-rules-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> StrategyRuleStore:
    return StrategyRuleStore.from_engine(engine)


def _request(**overrides: object) -> RegisterRuleRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "rule_id": "rule-1",
        "title": "Prefer short hooks",
        "rule_kind": "content-allocation",
        "parameters": {"max_hook_seconds": 3, "platform": "tiktok"},
    }
    payload.update(overrides)
    return RegisterRuleRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure lifecycle
# ---------------------------------------------------------------------------


def test_canary_promotion_rollback_lifecycle() -> None:
    assert rule_transition_allowed(RuleStatus.DRAFT, RuleStatus.CANARY)
    assert rule_transition_allowed(RuleStatus.CANARY, RuleStatus.ACTIVE)
    assert rule_transition_allowed(RuleStatus.CANARY, RuleStatus.ROLLED_BACK)
    assert rule_transition_allowed(RuleStatus.ACTIVE, RuleStatus.ROLLED_BACK)
    assert rule_transition_allowed(RuleStatus.ROLLED_BACK, RuleStatus.CANARY)


def test_cannot_skip_canary() -> None:
    assert not rule_transition_allowed(RuleStatus.DRAFT, RuleStatus.ACTIVE)
    assert not rule_transition_allowed(RuleStatus.DRAFT, RuleStatus.ROLLED_BACK)
    assert not rule_transition_allowed(RuleStatus.ACTIVE, RuleStatus.CANARY)
    assert not rule_transition_allowed(RuleStatus.ROLLED_BACK, RuleStatus.ACTIVE)


def test_same_state_noop_allowed() -> None:
    assert rule_transition_allowed(RuleStatus.ACTIVE, RuleStatus.ACTIVE)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_register_idempotent_conflict_audit(
    store: StrategyRuleStore,
    audit_log: Path,
) -> None:
    first = store.register_rule(_request())
    second = store.register_rule(_request())
    assert first.rule_id == second.rule_id
    assert first.status is RuleStatus.DRAFT
    assert first.parameters["max_hook_seconds"] == 3
    assert "strategy_rules.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_rule(_request(title="Changed"))
    assert len(store.list_rules("org-1")) == 1


def test_full_rollout_flow(store: StrategyRuleStore, audit_log: Path) -> None:
    store.register_rule(_request())
    canary = store.transition_rule(
        "rule-1", "org-1", RuleTransitionRequest(status=RuleStatus.CANARY, actor_ref="op-1")
    )
    assert canary.status is RuleStatus.CANARY
    promoted = store.transition_rule(
        "rule-1",
        "org-1",
        RuleTransitionRequest(status=RuleStatus.ACTIVE, actor_ref="op-1", reason_ref="eval-pass"),
    )
    assert promoted.status is RuleStatus.ACTIVE
    rolled = store.transition_rule(
        "rule-1",
        "org-1",
        RuleTransitionRequest(
            status=RuleStatus.ROLLED_BACK, actor_ref="op-1", reason_ref="regression"
        ),
    )
    assert rolled.status is RuleStatus.ROLLED_BACK
    recanary = store.transition_rule(
        "rule-1", "org-1", RuleTransitionRequest(status=RuleStatus.CANARY, actor_ref="op-1")
    )
    assert recanary.status is RuleStatus.CANARY
    log = audit_log.read_text(encoding="utf-8")
    assert log.count("strategy_rules.status_changed") == 4


def test_illegal_transitions_rejected(store: StrategyRuleStore) -> None:
    store.register_rule(_request())
    with pytest.raises(ValueError, match="illegal rule transition"):
        store.transition_rule(
            "rule-1", "org-1", RuleTransitionRequest(status=RuleStatus.ACTIVE, actor_ref="op")
        )
    store.transition_rule(
        "rule-1", "org-1", RuleTransitionRequest(status=RuleStatus.CANARY, actor_ref="op")
    )
    store.transition_rule(
        "rule-1", "org-1", RuleTransitionRequest(status=RuleStatus.ACTIVE, actor_ref="op")
    )
    with pytest.raises(ValueError, match="illegal rule transition"):
        store.transition_rule(
            "rule-1", "org-1", RuleTransitionRequest(status=RuleStatus.CANARY, actor_ref="op")
        )


def test_unknown_rule_404(store: StrategyRuleStore) -> None:
    with pytest.raises(ValueError, match="strategy rule not found"):
        store.transition_rule(
            "missing", "org-1", RuleTransitionRequest(status=RuleStatus.CANARY, actor_ref="op")
        )
    assert store.get_rule("missing", "org-1") is None


def test_list_status_filter(store: StrategyRuleStore) -> None:
    store.register_rule(_request())
    store.register_rule(_request(rule_id="rule-2", parameters={"variant": True}))
    store.transition_rule(
        "rule-2", "org-1", RuleTransitionRequest(status=RuleStatus.CANARY, actor_ref="op")
    )
    assert len(store.list_rules("org-1")) == 2
    assert len(store.list_rules("org-1", RuleStatus.DRAFT)) == 1
    assert len(store.list_rules("org-1", RuleStatus.CANARY)) == 1
    assert len(store.list_rules("org-other")) == 0


def test_same_state_transition_is_noop(store: StrategyRuleStore) -> None:
    store.register_rule(_request())
    again = store.transition_rule(
        "rule-1", "org-1", RuleTransitionRequest(status=RuleStatus.DRAFT, actor_ref="op")
    )
    assert again.status is RuleStatus.DRAFT


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/strategy-rules" in paths
    assert "/strategy-rules/{rule_id}" in paths
    assert "/strategy-rules/{rule_id}/transition" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "rule_id": "rule-1",
        "title": "Prefer short hooks",
        "rule_kind": "content-allocation",
        "parameters": {"max_hook_seconds": 3},
    }


def test_rule_route_roundtrip(client) -> None:
    created = client.post("/strategy-rules", json=_payload())
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "DRAFT"

    fetched = client.get("/strategy-rules/rule-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200

    missing = client.get("/strategy-rules/nope", params={"org_id": "org-1"})
    assert missing.status_code == 404

    conflict = client.post("/strategy-rules", json={**_payload(), "title": "Changed"})
    assert conflict.status_code == 409

    # canary -> promote -> rollback -> re-canary through routes.
    steps = ["CANARY", "ACTIVE", "ROLLED_BACK", "CANARY"]
    for status in steps:
        resp = client.post(
            "/strategy-rules/rule-1/transition",
            params={"org_id": "org-1"},
            json={"status": status, "actor_ref": "op-1"},
        )
        assert resp.status_code == 200, (status, resp.text)
        assert resp.json()["status"] == status

    listing = client.get("/strategy-rules", params={"org_id": "org-1", "status": "CANARY"})
    assert len(listing.json()) == 1


def test_skip_canary_409(client) -> None:
    client.post("/strategy-rules", json=_payload())
    resp = client.post(
        "/strategy-rules/rule-1/transition",
        params={"org_id": "org-1"},
        json={"status": "ACTIVE", "actor_ref": "op-1"},
    )
    assert resp.status_code == 409


def test_bad_slug_422(client) -> None:
    payload = _payload()
    payload["rule_id"] = "bad/id"
    resp = client.post("/strategy-rules", json=payload)
    assert resp.status_code == 422
