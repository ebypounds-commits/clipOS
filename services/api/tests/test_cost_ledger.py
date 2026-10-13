"""Tests for cost ledger/provider cost ingestion (CLIP-074)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.cost_ledger import (
    CostCategory,
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
    path = Path("/tmp") / "clipos-074-cost-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> CostLedgerStore:
    return CostLedgerStore.from_engine(engine)


def _source(**overrides: object) -> RegisterSourceRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "source_id": "csrc-1",
        "platform": "cloud",
        "account_ref": "acct://cloud-1",
        "cost_center": "compute",
    }
    payload.update(overrides)
    return RegisterSourceRequest(**payload)  # type: ignore[arg-type]


def _sync(**overrides: object) -> SyncCostsRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "source_id": "csrc-1",
        "period_start": date(2026, 10, 1),
        "period_end": date(2026, 10, 7),
    }
    payload.update(overrides)
    return SyncCostsRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


def test_mock_health() -> None:
    health = MockCostAdapter().health_check("acct")
    assert health.ok is True
    assert "fetch" in health.capabilities
    assert "mock" in (health.detail or "")


def test_mock_fetch_deterministic() -> None:
    adapter = MockCostAdapter()
    first = adapter.fetch_costs("acct", date(2026, 10, 1), date(2026, 10, 7))
    second = adapter.fetch_costs("acct", date(2026, 10, 1), date(2026, 10, 7))
    assert len(first) == 2
    assert [d.external_ref for d in first] == [d.external_ref for d in second]
    assert [d.amount_cents for d in first] == [d.amount_cents for d in second]
    assert all(d.amount_cents > 0 for d in first)
    assert {d.cost_category for d in first} == {
        CostCategory.USAGE,
        CostCategory.STORAGE,
    }


# ---------------------------------------------------------------------------
# Sources store
# ---------------------------------------------------------------------------


def test_register_source_idempotent_conflict_audit(
    store: CostLedgerStore,
    audit_log: Path,
) -> None:
    first = store.register_source(_source())
    again = store.register_source(_source())
    assert first.source_id == again.source_id
    assert "cost_sources.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_source(_source(account_ref="acct://other"))
    assert len(store.list_sources("org-1")) == 1


def test_get_list_org_scoped(store: CostLedgerStore) -> None:
    store.register_source(_source())
    assert store.get_source("csrc-1", "org-1") is not None
    assert store.get_source("csrc-1", "org-other") is None
    assert len(store.list_sources("org-other")) == 0


# ---------------------------------------------------------------------------
# Sync store
# ---------------------------------------------------------------------------


def test_sync_imports_then_skips(store: CostLedgerStore, audit_log: Path) -> None:
    store.register_source(_source())
    first = store.sync(MockCostAdapter(), _sync())
    assert first.imported == 2
    assert first.skipped == 0
    assert "cost_entries.recorded" in audit_log.read_text(encoding="utf-8")

    repeat = store.sync(MockCostAdapter(), _sync())
    assert repeat.imported == 0
    assert repeat.skipped == 2

    entries = store.list_entries("org-1", "csrc-1")
    assert len(entries) == 2
    assert all(isinstance(e.amount_cents, int) for e in entries)


def test_sync_unknown_source_404(store: CostLedgerStore) -> None:
    with pytest.raises(ValueError, match="cost source not found"):
        store.sync(MockCostAdapter(), _sync(source_id="missing"))


def test_sync_rejects_inverted_period(store: CostLedgerStore) -> None:
    store.register_source(_source())
    with pytest.raises(ValueError, match="period_end must not precede"):
        store.sync(
            MockCostAdapter(),
            _sync(period_start=date(2026, 10, 7), period_end=date(2026, 10, 1)),
        )


def test_sync_different_period_imports_new_entries(store: CostLedgerStore) -> None:
    store.register_source(_source())
    store.sync(MockCostAdapter(), _sync())
    week_two = store.sync(
        MockCostAdapter(),
        _sync(period_start=date(2026, 10, 8), period_end=date(2026, 10, 14)),
    )
    assert week_two.imported == 2
    assert len(store.list_entries("org-1")) == 4


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/cost-sources" in paths
    assert "/cost-sources/{source_id}" in paths
    assert "/cost-sync" in paths
    assert "/cost-entries" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "source_id": "csrc-1",
        "platform": "cloud",
        "account_ref": "acct://cloud-1",
        "cost_center": "compute",
    }


def test_source_and_sync_roundtrip(client) -> None:
    created = client.post("/cost-sources", json=_payload())
    assert created.status_code == 200, created.text

    fetched = client.get("/cost-sources/csrc-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200

    conflict = client.post("/cost-sources", json={**_payload(), "account_ref": "x"})
    assert conflict.status_code == 409

    sync = client.post(
        "/cost-sync",
        json={
            "org_id": "org-1",
            "source_id": "csrc-1",
            "period_start": "2026-10-01",
            "period_end": "2026-10-07",
        },
    )
    assert sync.status_code == 200, sync.text
    assert sync.json()["imported"] == 2

    repeat = client.post(
        "/cost-sync",
        json={
            "org_id": "org-1",
            "source_id": "csrc-1",
            "period_start": "2026-10-01",
            "period_end": "2026-10-07",
        },
    )
    assert repeat.json()["imported"] == 0
    assert repeat.json()["skipped"] == 2

    entries = client.get("/cost-entries", params={"org_id": "org-1", "source_id": "csrc-1"})
    assert len(entries.json()) == 2


def test_sync_unknown_source_404_route(client) -> None:
    resp = client.post(
        "/cost-sync",
        json={
            "org_id": "org-1",
            "source_id": "missing",
            "period_start": "2026-10-01",
            "period_end": "2026-10-07",
        },
    )
    assert resp.status_code == 404


def test_unknown_source_get_404(client) -> None:
    resp = client.get("/cost-sources/nope", params={"org_id": "org-1"})
    assert resp.status_code == 404


def test_bad_slug_422(client) -> None:
    payload = _payload()
    payload["source_id"] = "bad/id"
    resp = client.post("/cost-sources", json=payload)
    assert resp.status_code == 422
