"""Tests for revenue-source/ledger adapters (CLIP-070)."""

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


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-070-revenue-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> RevenueLedgerStore:
    return RevenueLedgerStore.from_engine(engine)


def _source(**overrides: object) -> RegisterSourceRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "source_id": "src-1",
        "platform": "youtube",
        "account_ref": "acct://yt-1",
        "revenue_kind": RevenueKind.PLATFORM,
    }
    payload.update(overrides)
    return RegisterSourceRequest(**payload)  # type: ignore[arg-type]


def _sync(**overrides: object) -> SyncRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "source_id": "src-1",
        "period_start": date(2026, 10, 1),
        "period_end": date(2026, 10, 7),
    }
    payload.update(overrides)
    return SyncRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


def test_mock_health() -> None:
    health = MockRevenueAdapter().health_check("acct")
    assert health.ok is True
    assert "fetch" in health.capabilities
    assert "mock" in (health.detail or "")


def test_mock_fetch_deterministic() -> None:
    adapter = MockRevenueAdapter()
    first = adapter.fetch_revenue("acct", date(2026, 10, 1), date(2026, 10, 7))
    second = adapter.fetch_revenue("acct", date(2026, 10, 1), date(2026, 10, 7))
    assert len(first) == 2
    assert [d.external_ref for d in first] == [d.external_ref for d in second]
    assert [d.amount_cents for d in first] == [d.amount_cents for d in second]
    assert all(d.amount_cents > 0 for d in first)
    assert all(d.currency == "USD" for d in first)
    assert first[0].revenue_kind is RevenueKind.PLATFORM
    assert first[1].revenue_kind is RevenueKind.AFFILIATE


def test_mock_fetch_different_period_different_refs() -> None:
    adapter = MockRevenueAdapter()
    week_one = adapter.fetch_revenue("acct", date(2026, 10, 1), date(2026, 10, 7))
    week_two = adapter.fetch_revenue("acct", date(2026, 10, 8), date(2026, 10, 14))
    assert week_one[0].external_ref != week_two[0].external_ref


# ---------------------------------------------------------------------------
# Sources store
# ---------------------------------------------------------------------------


def test_register_source_idempotent_conflict_audit(
    store: RevenueLedgerStore,
    audit_log: Path,
) -> None:
    first = store.register_source(_source())
    second = store.register_source(_source())
    assert first.source_id == second.source_id
    assert "revenue_sources.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_source(_source(account_ref="acct://other"))
    assert len(store.list_sources("org-1")) == 1


def test_get_list_org_scoped(store: RevenueLedgerStore) -> None:
    store.register_source(_source())
    assert store.get_source("src-1", "org-1") is not None
    assert store.get_source("src-1", "org-other") is None
    assert store.get_source("missing", "org-1") is None
    assert len(store.list_sources("org-other")) == 0


# ---------------------------------------------------------------------------
# Sync store
# ---------------------------------------------------------------------------


def test_sync_imports_then_skips(store: RevenueLedgerStore, audit_log: Path) -> None:
    store.register_source(_source())
    first = store.sync(MockRevenueAdapter(), _sync())
    assert first.imported == 2
    assert first.skipped == 0
    assert len(first.entries) == 2
    assert "revenue_entries.recorded" in audit_log.read_text(encoding="utf-8")

    repeat = store.sync(MockRevenueAdapter(), _sync())
    assert repeat.imported == 0
    assert repeat.skipped == 2

    entries = store.list_entries("org-1", "src-1")
    assert len(entries) == 2
    assert all(isinstance(e.amount_cents, int) for e in entries)


def test_sync_unknown_source_404(store: RevenueLedgerStore) -> None:
    with pytest.raises(ValueError, match="revenue source not found"):
        store.sync(MockRevenueAdapter(), _sync(source_id="missing"))


def test_sync_rejects_inverted_period(store: RevenueLedgerStore) -> None:
    store.register_source(_source())
    with pytest.raises(ValueError, match="period_end must not precede"):
        store.sync(
            MockRevenueAdapter(),
            _sync(period_start=date(2026, 10, 7), period_end=date(2026, 10, 1)),
        )


def test_sync_different_period_imports_new_entries(
    store: RevenueLedgerStore,
) -> None:
    store.register_source(_source())
    store.sync(MockRevenueAdapter(), _sync())
    week_two = store.sync(
        MockRevenueAdapter(),
        _sync(period_start=date(2026, 10, 8), period_end=date(2026, 10, 14)),
    )
    assert week_two.imported == 2
    assert len(store.list_entries("org-1")) == 4


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/revenue-sources" in paths
    assert "/revenue-sources/{source_id}" in paths
    assert "/revenue-sync" in paths
    assert "/revenue-entries" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "source_id": "src-1",
        "platform": "youtube",
        "account_ref": "acct://yt-1",
        "revenue_kind": "PLATFORM",
    }


def test_source_and_sync_roundtrip(client) -> None:
    created = client.post("/revenue-sources", json=_payload())
    assert created.status_code == 200, created.text

    fetched = client.get("/revenue-sources/src-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200

    listing = client.get("/revenue-sources", params={"org_id": "org-1"})
    assert len(listing.json()) == 1

    conflict = client.post("/revenue-sources", json={**_payload(), "account_ref": "x"})
    assert conflict.status_code == 409

    sync = client.post(
        "/revenue-sync",
        json={
            "org_id": "org-1",
            "source_id": "src-1",
            "period_start": "2026-10-01",
            "period_end": "2026-10-07",
        },
    )
    assert sync.status_code == 200, sync.text
    assert sync.json()["imported"] == 2

    repeat = client.post(
        "/revenue-sync",
        json={
            "org_id": "org-1",
            "source_id": "src-1",
            "period_start": "2026-10-01",
            "period_end": "2026-10-07",
        },
    )
    assert repeat.json()["imported"] == 0
    assert repeat.json()["skipped"] == 2

    entries = client.get("/revenue-entries", params={"org_id": "org-1", "source_id": "src-1"})
    assert len(entries.json()) == 2


def test_sync_unknown_source_404_route(client) -> None:
    resp = client.post(
        "/revenue-sync",
        json={
            "org_id": "org-1",
            "source_id": "missing",
            "period_start": "2026-10-01",
            "period_end": "2026-10-07",
        },
    )
    assert resp.status_code == 404


def test_unknown_source_get_404(client) -> None:
    resp = client.get("/revenue-sources/nope", params={"org_id": "org-1"})
    assert resp.status_code == 404


def test_bad_slug_422(client) -> None:
    payload = _payload()
    payload["source_id"] = "bad/id"
    resp = client.post("/revenue-sources", json=payload)
    assert resp.status_code == 422
