"""Tests for profitability/reinvestment calculations (CLIP-076)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.cost_ledger import (
    CostLedgerStore,
    MockCostAdapter,
    SyncCostsRequest,
)
from clipos_api.cost_ledger import (
    RegisterSourceRequest as RegisterCostSourceRequest,
)
from clipos_api.main import app
from clipos_api.profitability import (
    ProfitabilityStore,
    RunReportRequest,
    calculate_profitability,
    calculate_reinvestment,
)
from clipos_api.revenue_ledger import (
    MockRevenueAdapter,
    RevenueKind,
    RevenueLedgerStore,
)
from clipos_api.revenue_ledger import (
    RegisterSourceRequest as RegisterRevenueSourceRequest,
)
from clipos_api.revenue_ledger import (
    SyncRequest as RevenueSyncRequest,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-076-profit-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> ProfitabilityStore:
    return ProfitabilityStore.from_engine(engine)


PERIOD_START = date(2026, 10, 1)
PERIOD_END = date(2026, 10, 7)


def _report(**overrides: object) -> RunReportRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "report_id": "rep-1",
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
        "currency": "USD",
        "reinvest_pct": 50.0,
    }
    payload.update(overrides)
    return RunReportRequest(**payload)  # type: ignore[arg-type]


def _seed_ledgers(engine: Engine) -> tuple[int, int]:
    """Sync one revenue and one cost window; return (revenue, cost) cents."""
    revenue_store = RevenueLedgerStore.from_engine(engine)
    revenue_store.register_source(
        RegisterRevenueSourceRequest(
            org_id="org-1",
            source_id="rsrc-1",
            platform="youtube",
            account_ref="acct://yt",
            revenue_kind=RevenueKind.PLATFORM,
        )
    )
    revenue_store.sync(
        MockRevenueAdapter(),
        RevenueSyncRequest(
            org_id="org-1",
            source_id="rsrc-1",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        ),
    )
    revenue_total = sum(e.amount_cents for e in revenue_store.list_entries("org-1", "rsrc-1"))

    cost_store = CostLedgerStore.from_engine(engine)
    cost_store.register_source(
        RegisterCostSourceRequest(
            org_id="org-1",
            source_id="csrc-1",
            platform="cloud",
            account_ref="acct://cloud",
            cost_center="compute",
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
    cost_total = sum(e.amount_cents for e in cost_store.list_entries("org-1", "csrc-1"))
    return revenue_total, cost_total


# ---------------------------------------------------------------------------
# Pure profitability
# ---------------------------------------------------------------------------


def test_profitability_basic() -> None:
    result = calculate_profitability(10_000, 4_000)
    assert result.profit_cents == 6_000
    assert result.margin_pct == pytest.approx(60.0)
    assert result.profitable is True


def test_profitability_loss() -> None:
    result = calculate_profitability(1_000, 3_000)
    assert result.profit_cents == -2_000
    assert result.profitable is False
    assert result.margin_pct == pytest.approx(-200.0)


def test_profitability_zero_revenue() -> None:
    empty = calculate_profitability(0, 0)
    assert empty.margin_pct == 0.0
    assert empty.profitable is True
    cost_only = calculate_profitability(0, 500)
    assert cost_only.margin_pct == -100.0
    assert cost_only.profitable is False


# ---------------------------------------------------------------------------
# Pure reinvestment
# ---------------------------------------------------------------------------


def test_reinvestment_split_floor() -> None:
    split = calculate_reinvestment(1_001, 50.0)
    assert split.reinvest_cents == 500  # floor(500.5)
    assert split.reserve_cents == 501
    assert split.reason == "split_applied"
    assert split.reinvest_cents + split.reserve_cents == 1_001


def test_reinvestment_edges() -> None:
    all_in = calculate_reinvestment(1_000, 100.0)
    assert all_in.reinvest_cents == 1_000
    assert all_in.reserve_cents == 0
    all_reserve = calculate_reinvestment(1_000, 0.0)
    assert all_reserve.reinvest_cents == 0
    assert all_reserve.reserve_cents == 1_000


def test_reinvestment_non_positive_profit() -> None:
    split = calculate_reinvestment(-500, 50.0)
    assert split.reinvest_cents == 0
    assert split.reserve_cents == 0
    assert split.reason == "non_positive_profit"


def test_reinvestment_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="reinvest_pct"):
        calculate_reinvestment(1_000, 101.0)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_run_report_empty_window(
    engine: Engine, store: ProfitabilityStore, audit_log: Path
) -> None:
    report = store.run_report(_report())
    assert report.revenue_cents == 0
    assert report.cost_cents == 0
    assert report.profit_cents == 0
    assert report.profitable is True
    assert "profitability_reports.recorded" in audit_log.read_text(encoding="utf-8")


def test_run_report_with_ledgers(engine: Engine, store: ProfitabilityStore) -> None:
    revenue, cost = _seed_ledgers(engine)
    report = store.run_report(_report())
    assert report.revenue_cents == revenue
    assert report.cost_cents == cost
    assert report.profit_cents == revenue - cost
    assert report.reinvest_cents + report.reserve_cents == max(report.profit_cents, 0)


def test_run_report_idempotent_conflict(engine: Engine, store: ProfitabilityStore) -> None:
    first = store.run_report(_report())
    again = store.run_report(_report())
    assert first.report_id == again.report_id
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.run_report(_report(reinvest_pct=10.0))
    assert len(store.list_reports("org-1")) == 1


def test_run_report_inverted_period(store: ProfitabilityStore) -> None:
    with pytest.raises(ValueError, match="period_end must not precede"):
        store.run_report(_report(period_start=PERIOD_END, period_end=PERIOD_START))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/profitability/reports" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "report_id": "rep-1",
        "period_start": PERIOD_START.isoformat(),
        "period_end": PERIOD_END.isoformat(),
        "currency": "USD",
        "reinvest_pct": 50.0,
    }


def test_report_route_roundtrip(client) -> None:
    created = client.post("/profitability/reports", json=_payload())
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["report_id"] == "rep-1"
    assert body["reinvest_pct"] == 50.0

    repeat = client.post("/profitability/reports", json=_payload())
    assert repeat.status_code == 200

    listing = client.get("/profitability/reports", params={"org_id": "org-1"})
    assert len(listing.json()) == 1


def test_report_route_errors(client) -> None:
    conflict_payload = _payload()
    assert client.post("/profitability/reports", json=conflict_payload).status_code == 200
    changed = {**conflict_payload, "reinvest_pct": 10.0}
    assert client.post("/profitability/reports", json=changed).status_code == 409

    bad_slug = client.post("/profitability/reports", json={**_payload(), "report_id": "bad/id"})
    assert bad_slug.status_code == 422

    bad_pct = client.post("/profitability/reports", json={**_payload(), "reinvest_pct": 150.0})
    assert bad_pct.status_code == 422
