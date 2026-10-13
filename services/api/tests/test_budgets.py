"""Tests for budgets/spend authorization (CLIP-075)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.budgets import (
    AuthorizeRequest,
    BudgetStore,
    RegisterBudgetRequest,
    authorize_spend,
)
from clipos_api.cost_ledger import (
    CostLedgerStore,
    MockCostAdapter,
    RegisterSourceRequest,
    SyncCostsRequest,
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
    path = Path("/tmp") / "clipos-075-budget-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> BudgetStore:
    return BudgetStore.from_engine(engine)


@pytest.fixture()
def cost_store(engine: Engine) -> CostLedgerStore:
    return CostLedgerStore.from_engine(engine)


PERIOD_START = date(2026, 10, 1)
PERIOD_END = date(2026, 10, 7)


def _budget(**overrides: object) -> RegisterBudgetRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "budget_id": "bud-1",
        "scope_key": "compute",
        "amount_cents": 100_000,
        "currency": "USD",
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
    }
    payload.update(overrides)
    return RegisterBudgetRequest(**payload)  # type: ignore[arg-type]


def _authorize(**overrides: object) -> AuthorizeRequest:
    payload: dict[str, object] = {
        "authorization_id": "auth-1",
        "amount_cents": 1_000,
        "currency": "USD",
        "actor_ref": "op-1",
    }
    payload.update(overrides)
    return AuthorizeRequest(**payload)  # type: ignore[arg-type]


def _seed_spend(cost_store: CostLedgerStore, scope: str = "compute") -> int:
    """Sync one window for a scope; return recorded total cents."""
    cost_store.register_source(
        RegisterSourceRequest(
            org_id="org-1",
            source_id="csrc-1",
            platform="cloud",
            account_ref="acct://cloud",
            cost_center=scope,
        )
    )
    cost_store.sync(
        MockCostAdapter(),
        SyncCostsRequest(
            org_id="org-1",
            source_id="csrc-1",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        ),
    )
    entries = cost_store.list_entries("org-1", "csrc-1")
    return sum(e.amount_cents for e in entries)


# ---------------------------------------------------------------------------
# Pure authorization
# ---------------------------------------------------------------------------


def test_authorize_within_budget() -> None:
    decision = authorize_spend(10_000, 2_000, 3_000)
    assert decision.allowed is True
    assert decision.reason == "within_budget"
    assert decision.remaining_before == 8_000
    assert decision.remaining_after == 5_000


def test_authorize_exact_boundary_allowed() -> None:
    decision = authorize_spend(10_000, 5_000, 5_000)
    assert decision.allowed is True
    assert decision.remaining_after == 0


def test_authorize_exceeds_budget() -> None:
    decision = authorize_spend(10_000, 9_500, 1_000)
    assert decision.allowed is False
    assert decision.reason == "exceeds_budget"
    assert decision.remaining_after is None


def test_authorize_zero_requested_with_full_spend() -> None:
    decision = authorize_spend(10_000, 10_000, 0)
    assert decision.allowed is True
    assert decision.remaining_after == 0


# ---------------------------------------------------------------------------
# Budget store
# ---------------------------------------------------------------------------


def test_budget_register_idempotent_conflict_audit(
    store: BudgetStore,
    audit_log: Path,
) -> None:
    first = store.register_budget(_budget())
    again = store.register_budget(_budget())
    assert first.budget_id == again.budget_id
    assert "budgets.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_budget(_budget(amount_cents=1))
    assert len(store.list_budgets("org-1")) == 1


def test_budget_inverted_period_rejected(store: BudgetStore) -> None:
    with pytest.raises(ValueError, match="period_end must not precede"):
        store.register_budget(_budget(period_start=PERIOD_END, period_end=PERIOD_START))


def test_authorize_without_spend_allows(
    store: BudgetStore,
    audit_log: Path,
) -> None:
    store.register_budget(_budget())
    decision = store.authorize("bud-1", "org-1", _authorize())
    assert decision.allowed is True
    assert decision.spent_before_cents == 0
    assert decision.cap_cents == 100_000
    assert "spend_authorizations.evaluated" in audit_log.read_text(encoding="utf-8")


def test_authorize_counts_recorded_spend(
    store: BudgetStore,
    cost_store: CostLedgerStore,
) -> None:
    spent = _seed_spend(cost_store)
    store.register_budget(_budget(amount_cents=spent))
    # Cap equals spent: any positive request exceeds.
    denied = store.authorize("bud-1", "org-1", _authorize(amount_cents=1))
    assert denied.allowed is False
    assert denied.reason == "exceeds_budget"
    assert denied.spent_before_cents == spent

    zero = store.authorize("bud-1", "org-1", _authorize(authorization_id="auth-2", amount_cents=0))
    assert zero.allowed is True


def test_authorize_scope_filters_spend(
    store: BudgetStore,
    cost_store: CostLedgerStore,
) -> None:
    # Spend recorded under scope 'egress'; budget scope is 'compute'.
    _seed_spend(cost_store, scope="egress")
    store.register_budget(_budget())
    decision = store.authorize("bud-1", "org-1", _authorize())
    assert decision.allowed is True
    assert decision.spent_before_cents == 0


def test_authorize_currency_mismatch_denies(
    store: BudgetStore,
    cost_store: CostLedgerStore,
) -> None:
    store.register_budget(_budget())
    decision = store.authorize("bud-1", "org-1", _authorize(currency="EUR"))
    assert decision.allowed is False
    assert decision.reason == "currency_mismatch"


def test_authorize_idempotent_conflict(store: BudgetStore) -> None:
    store.register_budget(_budget())
    first = store.authorize("bud-1", "org-1", _authorize())
    again = store.authorize("bud-1", "org-1", _authorize())
    assert first.authorization_id == again.authorization_id
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.authorize("bud-1", "org-1", _authorize(amount_cents=9_999))
    assert len(store.list_authorizations("org-1", "bud-1")) == 1


def test_authorize_unknown_budget_404(store: BudgetStore) -> None:
    with pytest.raises(ValueError, match="budget not found"):
        store.authorize("missing", "org-1", _authorize())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/budgets" in paths
    assert "/budgets/{budget_id}" in paths
    assert "/budgets/{budget_id}/authorize" in paths
    assert "/budgets/{budget_id}/authorizations" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient
    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "budget_id": "bud-1",
        "scope_key": "compute",
        "amount_cents": 100_000,
        "currency": "USD",
        "period_start": PERIOD_START.isoformat(),
        "period_end": PERIOD_END.isoformat(),
    }


def test_budget_route_roundtrip(client) -> None:
    created = client.post("/budgets", json=_payload())
    assert created.status_code == 200, created.text

    fetched = client.get("/budgets/bud-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200

    listing = client.get("/budgets", params={"org_id": "org-1"})
    assert len(listing.json()) == 1

    authorized = client.post(
        "/budgets/bud-1/authorize",
        params={"org_id": "org-1"},
        json={
            "authorization_id": "auth-1",
            "amount_cents": 1_000,
            "currency": "USD",
            "actor_ref": "op-1",
        },
    )
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["allowed"] is True

    decisions = client.get("/budgets/bud-1/authorizations", params={"org_id": "org-1"})
    assert len(decisions.json()) == 1


def test_budget_route_errors(client) -> None:
    missing = client.get("/budgets/nope", params={"org_id": "org-1"})
    assert missing.status_code == 404

    assert client.post("/budgets", json=_payload()).status_code == 200
    conflict = client.post("/budgets", json={**_payload(), "amount_cents": 1})
    assert conflict.status_code == 409

    denied = client.post(
        "/budgets/bud-1/authorize",
        params={"org_id": "org-1"},
        json={
            "authorization_id": "auth-deny",
            "amount_cents": 1,
            "currency": "EUR",
            "actor_ref": "op-1",
        },
    )
    assert denied.status_code == 200
    assert denied.json()["allowed"] is False
    assert denied.json()["reason"] == "currency_mismatch"

    unknown = client.post(
        "/budgets/missing/authorize",
        params={"org_id": "org-1"},
        json={
            "authorization_id": "auth-x",
            "amount_cents": 1,
            "currency": "USD",
            "actor_ref": "op-1",
        },
    )
    assert unknown.status_code == 404

    bad_slug = client.post("/budgets", json={**_payload(), "budget_id": "bad/id"})
    assert bad_slug.status_code == 422
