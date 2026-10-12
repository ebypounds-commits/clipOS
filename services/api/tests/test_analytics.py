"""Tests for analytics provider contract (CLIP-062)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.analytics import (
    AnalyticsMetric,
    AnalyticsStore,
    CollectRequest,
    MetricKind,
    MockAnalyticsAdapter,
    collect_analytics,
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
    path = Path("/tmp") / "clipos-062-analytics-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> AnalyticsStore:
    return AnalyticsStore.from_engine(engine)


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


def test_mock_adapter_health() -> None:
    adapter = MockAnalyticsAdapter()
    health = adapter.health_check("acct-1")
    assert health.ok is True
    assert "collect" in health.capabilities
    assert "mock" in (health.detail or "").lower()


def test_mock_adapter_collect_deterministic() -> None:
    adapter = MockAnalyticsAdapter()
    metrics = adapter.collect_metrics("acct-1", "content://vid.mp4", "ext-1")
    assert len(metrics) == 3
    kinds = {m.kind for m in metrics}
    assert MetricKind.VIEW in kinds
    assert MetricKind.LIKE in kinds
    assert MetricKind.COMMENT in kinds
    for m in metrics:
        assert m.value > 0
        assert m.unit == "count"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_store_metrics_and_retrieve(store: AnalyticsStore) -> None:
    adapter = MockAnalyticsAdapter()
    metrics = adapter.collect_metrics("acct-1", "content://vid.mp4", "ext-1")
    snapshot = store.store_metrics("org-1", "youtube", "content://vid.mp4", metrics)
    assert snapshot.org_id == "org-1"
    assert snapshot.platform == "youtube"
    assert len(snapshot.metrics) == 3

    stored = store.get_metrics("org-1", "content://vid.mp4")
    assert len(stored) == 3


def test_store_metrics_idempotent_same_window(store: AnalyticsStore) -> None:
    now = datetime.now(UTC)
    metrics = [
        AnalyticsMetric(kind=MetricKind.VIEW, value=100.0, unit="count", collected_at=now),
    ]
    store.store_metrics("org-1", "youtube", "content://v.mp4", metrics)
    store.store_metrics("org-1", "youtube", "content://v.mp4", metrics)
    stored = store.get_metrics("org-1", "content://v.mp4")
    assert len(stored) == 1
    assert stored[0].value == 100.0


def test_store_metrics_different_kinds(store: AnalyticsStore) -> None:
    now = datetime.now(UTC)
    metrics = [
        AnalyticsMetric(kind=MetricKind.VIEW, value=100.0, unit="count", collected_at=now),
        AnalyticsMetric(kind=MetricKind.LIKE, value=10.0, unit="count", collected_at=now),
    ]
    store.store_metrics("org-1", "youtube", "content://v.mp4", metrics)
    views = store.get_metrics("org-1", "content://v.mp4", kind=MetricKind.VIEW)
    assert len(views) == 1
    assert views[0].kind == MetricKind.VIEW


def test_store_records_audit(store: AnalyticsStore, audit_log: Path) -> None:
    metrics = [AnalyticsMetric(kind=MetricKind.VIEW, value=50.0, unit="count")]
    store.store_metrics("org-1", "tiktok", "content://v.mp4", metrics)
    assert "analytics_metrics.collected" in audit_log.read_text(encoding="utf-8")


def test_get_metrics_empty_for_unknown_content(store: AnalyticsStore) -> None:
    assert store.get_metrics("org-1", "content://missing.mp4") == []


def test_get_metrics_org_scoped(store: AnalyticsStore) -> None:
    metrics = [AnalyticsMetric(kind=MetricKind.VIEW, value=50.0, unit="count")]
    store.store_metrics("org-1", "youtube", "content://v.mp4", metrics)
    assert len(store.get_metrics("org-1", "content://v.mp4")) == 1
    assert len(store.get_metrics("org-other", "content://v.mp4")) == 0


# ---------------------------------------------------------------------------
# Collect flow
# ---------------------------------------------------------------------------


def test_collect_analytics_flow(store: AnalyticsStore) -> None:
    adapter = MockAnalyticsAdapter()
    request = CollectRequest(
        org_id="org-1",
        platform="youtube",
        account_ref="yt-1",
        content_ref="content://vid.mp4",
        external_id="yt-mock-abc",
    )
    snapshot = collect_analytics(adapter, store, request)
    assert len(snapshot.metrics) == 3
    assert snapshot.snapshot_id

    stored = store.get_metrics("org-1", "content://vid.mp4")
    assert len(stored) == 3


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/analytics/collect" in paths
    assert "/analytics/metrics" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_collect_and_query_roundtrip(client) -> None:
    resp = client.post(
        "/analytics/collect",
        json={
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt-1",
            "content_ref": "content://vid.mp4",
            "external_id": "yt-mock-abc",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["metrics"]) == 3

    query = client.get(
        "/analytics/metrics",
        params={"org_id": "org-1", "content_ref": "content://vid.mp4"},
    )
    assert query.status_code == 200
    assert len(query.json()) == 3


def test_query_with_kind_filter(client) -> None:
    client.post(
        "/analytics/collect",
        json={
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt-1",
            "content_ref": "content://vid2.mp4",
            "external_id": "yt-mock-def",
        },
    )
    resp = client.get(
        "/analytics/metrics",
        params={"org_id": "org-1", "content_ref": "content://vid2.mp4", "kind": "VIEW"},
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["kind"] == "VIEW"


def test_query_empty_for_unknown(client) -> None:
    resp = client.get(
        "/analytics/metrics",
        params={"org_id": "org-1", "content_ref": "content://missing.mp4"},
    )
    assert resp.status_code == 200
    assert resp.json() == []
