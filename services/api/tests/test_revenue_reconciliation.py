"""Tests for revenue dashboards/reconciliation (CLIP-073)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.revenue_ledger import (
    MockRevenueAdapter,
    RegisterSourceRequest,
    RevenueKind,
    RevenueLedgerStore,
    SyncRequest,
)
from clipos_api.revenue_reconciliation import (
    ReconciliationStatus,
    RevenueDashboardStore,
    RunReconciliationRequest,
    reconcile_revenue,
    summarize_revenue,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-073-recon-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> RevenueDashboardStore:
    return RevenueDashboardStore.from_engine(engine)


@pytest.fixture()
def ledger(engine: Engine) -> RevenueLedgerStore:
    return RevenueLedgerStore.from_engine(engine)


PERIOD_START = date(2026, 10, 1)
PERIOD_END = date(2026, 10, 7)


def _seed_ledger(ledger: RevenueLedgerStore) -> int:
    """Register the source, sync one period, return recorded total cents."""
    ledger.register_source(
        RegisterSourceRequest(
            org_id="org-1",
            source_id="src-1",
            platform="youtube",
            account_ref="acct://yt",
            revenue_kind=RevenueKind.PLATFORM,
        )
    )
    ledger.sync(
        MockRevenueAdapter(),
        SyncRequest(
            org_id="org-1",
            source_id="src-1",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        ),
    )
    entries = ledger.list_entries("org-1", "src-1")
    return sum(e.amount_cents for e in entries)


def _recon(**overrides: object) -> RunReconciliationRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "reconciliation_id": "rec-1",
        "source_id": "src-1",
        "currency": "USD",
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
        "expected_cents": 1,
        "actor_ref": "op-1",
    }
    payload.update(overrides)
    return RunReconciliationRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_exact_match() -> None:
    result = reconcile_revenue(1000, 1000)
    assert result.status is ReconciliationStatus.MATCH
    assert result.delta_cents == 0
    assert result.delta_pct == 0.0


def test_reconcile_discrepancy() -> None:
    result = reconcile_revenue(900, 1000)
    assert result.status is ReconciliationStatus.DISCREPANCY
    assert result.delta_cents == -100
    assert result.delta_pct == pytest.approx(-10.0)


def test_reconcile_within_tolerance() -> None:
    result = reconcile_revenue(1005, 1000, tolerance_pct=1.0)
    assert result.status is ReconciliationStatus.MATCH
    assert result.delta_pct == pytest.approx(0.5)


def test_reconcile_zero_expected() -> None:
    both_zero = reconcile_revenue(0, 0)
    assert both_zero.status is ReconciliationStatus.MATCH
    assert both_zero.delta_pct == 0.0
    recorded_only = reconcile_revenue(500, 0)
    assert recorded_only.status is ReconciliationStatus.DISCREPANCY
    assert recorded_only.delta_pct == 100.0


# ---------------------------------------------------------------------------
# Pure dashboard
# ---------------------------------------------------------------------------


def test_dashboard_empty() -> None:
    summary = summarize_revenue("org-1", [])
    assert summary.entry_count == 0
    assert summary.total_cents_by_currency == {}


def test_dashboard_totals_by_currency_kind_source() -> None:
    from clipos_api.revenue_ledger import RevenueEntry

    entries = [
        RevenueEntry(
            entry_id="e1",
            org_id="org-1",
            source_id="src-1",
            external_ref="x1",
            revenue_kind=RevenueKind.PLATFORM,
            amount_cents=100,
            currency="USD",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        ),
        RevenueEntry(
            entry_id="e2",
            org_id="org-1",
            source_id="src-2",
            external_ref="x2",
            revenue_kind=RevenueKind.AFFILIATE,
            amount_cents=50,
            currency="USD",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        ),
        RevenueEntry(
            entry_id="e3",
            org_id="org-1",
            source_id="src-1",
            external_ref="x3",
            revenue_kind=RevenueKind.PLATFORM,
            amount_cents=80,
            currency="EUR",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        ),
    ]
    summary = summarize_revenue("org-1", entries)
    assert summary.entry_count == 3
    assert summary.total_cents_by_currency == {"USD": 150, "EUR": 80}
    assert summary.total_cents_by_kind == {
        "PLATFORM:USD": 100,
        "AFFILIATE:USD": 50,
        "PLATFORM:EUR": 80,
    }
    assert summary.total_cents_by_source == {"src-1:USD": 100, "src-2:USD": 50, "src-1:EUR": 80}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_dashboard_from_synced_ledger(
    store: RevenueDashboardStore, ledger: RevenueLedgerStore
) -> None:
    _seed_ledger(ledger)
    summary = store.dashboard("org-1")
    assert summary.entry_count == 2
    assert sum(summary.total_cents_by_currency.values()) > 0

    windowed = store.dashboard("org-1", PERIOD_START, PERIOD_END)
    assert windowed.entry_count == 2
    outside = store.dashboard("org-1", date(2026, 1, 1), date(2026, 1, 7))
    assert outside.entry_count == 0


def test_run_reconciliation_match_and_discrepancy(
    store: RevenueDashboardStore,
    ledger: RevenueLedgerStore,
    audit_log: Path,
) -> None:
    recorded = _seed_ledger(ledger)
    match = store.run_reconciliation(_recon(expected_cents=recorded))
    assert match.status is ReconciliationStatus.MATCH
    assert match.recorded_cents == recorded
    assert "revenue_reconciliations.recorded" in audit_log.read_text(encoding="utf-8")

    discrepant = store.run_reconciliation(
        _recon(reconciliation_id="rec-2", expected_cents=recorded + 500)
    )
    assert discrepant.status is ReconciliationStatus.DISCREPANCY
    assert discrepant.delta_cents == -500

    assert len(store.list_reconciliations("org-1", "src-1")) == 2
    assert len(store.list_reconciliations("org-other")) == 0


def test_reconciliation_idempotent_conflict(
    store: RevenueDashboardStore,
    ledger: RevenueLedgerStore,
) -> None:
    recorded = _seed_ledger(ledger)
    first = store.run_reconciliation(_recon(expected_cents=recorded))
    again = store.run_reconciliation(_recon(expected_cents=recorded))
    assert first.reconciliation_id == again.reconciliation_id
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.run_reconciliation(_recon(expected_cents=recorded + 1))


def test_reconciliation_requires_source(store: RevenueDashboardStore) -> None:
    with pytest.raises(ValueError, match="revenue source not found"):
        store.run_reconciliation(_recon(source_id="missing"))


def test_reconciliation_rejects_inverted_period(store: RevenueDashboardStore) -> None:
    with pytest.raises(ValueError, match="period_end must not precede"):
        store.run_reconciliation(_recon(period_start=PERIOD_END, period_end=PERIOD_START))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/revenue/dashboard" in paths
    assert "/revenue-reconciliations" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _seed_via_routes(client, ledger: RevenueLedgerStore) -> int:
    created = client.post(
        "/revenue-sources",
        json={
            "org_id": "org-1",
            "source_id": "src-1",
            "platform": "youtube",
            "account_ref": "acct://yt",
            "revenue_kind": "PLATFORM",
        },
    )
    assert created.status_code == 200, created.text
    sync = client.post(
        "/revenue-sync",
        json={
            "org_id": "org-1",
            "source_id": "src-1",
            "period_start": PERIOD_START.isoformat(),
            "period_end": PERIOD_END.isoformat(),
        },
    )
    assert sync.status_code == 200, sync.text
    entries = ledger.list_entries("org-1", "src-1")
    return sum(e.amount_cents for e in entries)


def test_dashboard_route(engine: Engine, client) -> None:
    ledger = RevenueLedgerStore.from_engine(engine)
    _seed_via_routes(client, ledger)
    resp = client.get("/revenue/dashboard", params={"org_id": "org-1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entry_count"] == 2
    assert sum(body["total_cents_by_currency"].values()) > 0

    windowed = client.get(
        "/revenue/dashboard",
        params={
            "org_id": "org-1",
            "period_start": PERIOD_START.isoformat(),
            "period_end": PERIOD_END.isoformat(),
        },
    )
    assert windowed.json()["entry_count"] == 2


def test_reconciliation_routes(engine: Engine, client) -> None:
    ledger = RevenueLedgerStore.from_engine(engine)
    recorded = _seed_via_routes(client, ledger)

    match = client.post(
        "/revenue-reconciliations",
        json={
            "org_id": "org-1",
            "reconciliation_id": "rec-1",
            "source_id": "src-1",
            "currency": "USD",
            "period_start": PERIOD_START.isoformat(),
            "period_end": PERIOD_END.isoformat(),
            "expected_cents": recorded,
            "actor_ref": "op-1",
        },
    )
    assert match.status_code == 200, match.text
    assert match.json()["status"] == "MATCH"

    missing = client.post(
        "/revenue-reconciliations",
        json={
            "org_id": "org-1",
            "reconciliation_id": "rec-2",
            "source_id": "missing",
            "currency": "USD",
            "period_start": PERIOD_START.isoformat(),
            "period_end": PERIOD_END.isoformat(),
            "expected_cents": 0,
            "actor_ref": "op-1",
        },
    )
    assert missing.status_code == 404

    listing = client.get("/revenue-reconciliations", params={"org_id": "org-1"})
    assert len(listing.json()) == 1
