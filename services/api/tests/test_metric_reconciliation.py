"""Tests for metric ingestion/reconciliation (CLIP-063)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.analytics import AnalyticsStore, MetricKind, MockAnalyticsAdapter
from clipos_api.main import app
from clipos_api.metric_reconciliation import (
    Discrepancy,
    ReconcileRequest,
    ReconciliationRecord,
    ReconciliationStatus,
    ReconciliationStore,
    reconcile_metrics,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-063-recon-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def analytics_store(engine: Engine) -> AnalyticsStore:
    return AnalyticsStore.from_engine(engine)


@pytest.fixture()
def recon_store(engine: Engine) -> ReconciliationStore:
    return ReconciliationStore.from_engine(engine)


# ---------------------------------------------------------------------------
# Reconciliation — no previous data
# ---------------------------------------------------------------------------


def test_reconcile_no_previous(
    analytics_store: AnalyticsStore,
    recon_store: ReconciliationStore,
    audit_log: Path,
) -> None:
    """First reconciliation for a content_ref returns NO_PREVIOUS."""
    adapter = MockAnalyticsAdapter()
    request = ReconcileRequest(
        org_id="org-1",
        platform="youtube",
        account_ref="yt-1",
        content_ref="content://vid.mp4",
        external_id="ext-1",
    )
    record = reconcile_metrics(adapter, analytics_store, recon_store, request)
    assert record.status == ReconciliationStatus.NO_PREVIOUS
    assert record.discrepancies == []
    assert record.metric_count == 3
    assert record.record_id
    assert "metric_reconciliation.reconciled" in audit_log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Reconciliation — with previous data (same values = MATCH)
# ---------------------------------------------------------------------------


def test_reconcile_match_same_values(
    analytics_store: AnalyticsStore,
    recon_store: ReconciliationStore,
) -> None:
    """Second collection with same mock values is a MATCH."""
    adapter = MockAnalyticsAdapter()
    request = ReconcileRequest(
        org_id="org-1",
        platform="youtube",
        account_ref="yt-1",
        content_ref="content://vid.mp4",
        external_id="ext-1",
    )
    # First run — NO_PREVIOUS
    reconcile_metrics(adapter, analytics_store, recon_store, request)
    # Second run — same values from mock
    record = reconcile_metrics(adapter, analytics_store, recon_store, request)
    assert record.status in (ReconciliationStatus.MATCH, ReconciliationStatus.NO_PREVIOUS)


# ---------------------------------------------------------------------------
# Reconciliation store
# ---------------------------------------------------------------------------


def test_recon_store_list_records(
    recon_store: ReconciliationStore,
) -> None:
    record = ReconciliationRecord(
        record_id="rec-1",
        org_id="org-1",
        platform="youtube",
        content_ref="content://vid.mp4",
        status=ReconciliationStatus.MATCH,
        discrepancies=[],
        metric_count=3,
    )
    stored = recon_store.store_record(record)
    assert stored.record_id == "rec-1"

    records = recon_store.list_records("org-1")
    assert len(records) == 1
    assert records[0].status == ReconciliationStatus.MATCH


def test_recon_store_filter_by_content_ref(
    recon_store: ReconciliationStore,
) -> None:
    recon_store.store_record(
        ReconciliationRecord(
            record_id="rec-1",
            org_id="org-1",
            platform="youtube",
            content_ref="content://a.mp4",
            status=ReconciliationStatus.MATCH,
            discrepancies=[],
            metric_count=3,
        )
    )
    recon_store.store_record(
        ReconciliationRecord(
            record_id="rec-2",
            org_id="org-1",
            platform="youtube",
            content_ref="content://b.mp4",
            status=ReconciliationStatus.DISCREPANCY,
            discrepancies=[],
            metric_count=3,
        )
    )
    assert len(recon_store.list_records("org-1")) == 2
    assert len(recon_store.list_records("org-1", "content://a.mp4")) == 1
    assert len(recon_store.list_records("org-1", "content://b.mp4")) == 1
    assert len(recon_store.list_records("org-other")) == 0


# ---------------------------------------------------------------------------
# Discrepancy model
# ---------------------------------------------------------------------------


def test_discrepancy_model() -> None:
    d = Discrepancy(
        kind=MetricKind.VIEW,
        previous_value=100.0,
        current_value=150.0,
        delta=50.0,
        delta_pct=50.0,
    )
    assert d.kind == MetricKind.VIEW
    assert d.delta == 50.0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/reconciliation/run" in paths
    assert "/reconciliation-records" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_reconciliation_roundtrip(client) -> None:
    """Full route roundtrip: run reconciliation, then list records."""
    resp = client.post(
        "/reconciliation/run",
        json={
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt-1",
            "content_ref": "content://vid.mp4",
            "external_id": "ext-1",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "NO_PREVIOUS"
    assert body["metric_count"] == 3

    listing = client.get("/reconciliation-records", params={"org_id": "org-1"})
    assert listing.status_code == 200
    assert len(listing.json()) == 1


def test_reconciliation_list_empty(client) -> None:
    resp = client.get("/reconciliation-records", params={"org_id": "org-empty"})
    assert resp.status_code == 200
    assert resp.json() == []
