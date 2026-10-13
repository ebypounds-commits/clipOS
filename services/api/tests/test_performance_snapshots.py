"""Tests for normalized performance snapshots (CLIP-064)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.analytics import AnalyticsStore, MetricKind, MockAnalyticsAdapter
from clipos_api.main import app
from clipos_api.performance_snapshots import (
    PerformanceSnapshotStore,
    SnapshotRequest,
    build_performance_snapshot,
    build_snapshot_fields,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-064-perf-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def analytics_store(engine: Engine) -> AnalyticsStore:
    return AnalyticsStore.from_engine(engine)


@pytest.fixture()
def snapshot_store(engine: Engine) -> PerformanceSnapshotStore:
    return PerformanceSnapshotStore.from_engine(engine)


# ---------------------------------------------------------------------------
# Pure normalization
# ---------------------------------------------------------------------------


def test_build_snapshot_fields_full() -> None:
    fields = build_snapshot_fields(
        {
            MetricKind.VIEW: 100.0,
            MetricKind.LIKE: 10.0,
            MetricKind.COMMENT: 5.0,
            MetricKind.SHARE: 5.0,
            MetricKind.IMPRESSION: 1000.0,
            MetricKind.WATCH_TIME_MINUTES: 42.0,
            MetricKind.CLICK_THROUGH: 3.0,
        }
    )
    assert fields["views"] == 100.0
    assert fields["likes"] == 10.0
    assert fields["engagement_rate"] == pytest.approx(20.0)


def test_build_snapshot_fields_missing_kinds_default_zero() -> None:
    fields = build_snapshot_fields({MetricKind.VIEW: 100.0})
    assert fields["views"] == 100.0
    assert fields["likes"] == 0.0
    assert fields["impressions"] == 0.0
    assert fields["engagement_rate"] == 0.0


def test_build_snapshot_fields_zero_views() -> None:
    fields = build_snapshot_fields({MetricKind.LIKE: 10.0})
    assert fields["engagement_rate"] == 0.0
    assert fields["views"] == 0.0


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _fields() -> dict[str, float]:
    return {
        "views": 100.0,
        "likes": 10.0,
        "comments": 5.0,
        "shares": 5.0,
        "impressions": 1000.0,
        "watch_time_minutes": 42.0,
        "click_throughs": 3.0,
        "engagement_rate": 20.0,
    }


def test_record_snapshot_idempotent(snapshot_store: PerformanceSnapshotStore) -> None:
    snapshot_store.record_snapshot("org-1", "youtube", "content://v.mp4", _fields(), "snap-1")
    again = snapshot_store.record_snapshot(
        "org-1", "youtube", "content://v.mp4", _fields(), "snap-2"
    )
    assert again.snapshot_id == "snap-1"
    assert len(snapshot_store.list_snapshots("org-1")) == 1


def test_record_snapshot_conflict(snapshot_store: PerformanceSnapshotStore) -> None:
    snapshot_store.record_snapshot("org-1", "youtube", "content://v.mp4", _fields(), "snap-1")
    changed = _fields()
    changed["views"] = 999.0
    with pytest.raises(ValueError, match="already recorded with different fields"):
        snapshot_store.record_snapshot("org-1", "youtube", "content://v.mp4", changed, "snap-2")


def test_record_snapshot_audited(
    snapshot_store: PerformanceSnapshotStore,
    audit_log: Path,
) -> None:
    snapshot_store.record_snapshot("org-1", "youtube", "content://v.mp4", _fields(), "snap-1")
    assert "performance_snapshots.recorded" in audit_log.read_text(encoding="utf-8")


def test_get_and_list_org_scoped(snapshot_store: PerformanceSnapshotStore) -> None:
    snapshot_store.record_snapshot("org-1", "youtube", "content://v.mp4", _fields(), "snap-1")
    assert snapshot_store.get_snapshot("org-1", "content://v.mp4") is not None
    assert snapshot_store.get_snapshot("org-other", "content://v.mp4") is None
    assert len(snapshot_store.list_snapshots("org-1")) == 1


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------


def test_build_performance_snapshot_flow(
    analytics_store: AnalyticsStore,
    snapshot_store: PerformanceSnapshotStore,
) -> None:
    adapter = MockAnalyticsAdapter()
    metrics = adapter.collect_metrics("acct", "content://v.mp4", "ext-1")
    analytics_store.store_metrics("org-1", "youtube", "content://v.mp4", metrics)

    snapshot = build_performance_snapshot(
        analytics_store,
        snapshot_store,
        SnapshotRequest(org_id="org-1", platform="youtube", content_ref="content://v.mp4"),
    )
    assert snapshot.views == 100.0
    assert snapshot.engagement_rate > 0


def test_build_performance_snapshot_no_metrics_404(
    analytics_store: AnalyticsStore,
    snapshot_store: PerformanceSnapshotStore,
) -> None:
    with pytest.raises(ValueError, match="not found"):
        build_performance_snapshot(
            analytics_store,
            snapshot_store,
            SnapshotRequest(
                org_id="org-1", platform="youtube", content_ref="content://missing.mp4"
            ),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/performance-snapshots" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_snapshot_roundtrip_via_route(client) -> None:
    client.post(
        "/analytics/collect",
        json={
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt-1",
            "content_ref": "content://vid.mp4",
            "external_id": "ext-1",
        },
    )
    built = client.post(
        "/performance-snapshots",
        json={"org_id": "org-1", "platform": "youtube", "content_ref": "content://vid.mp4"},
    )
    assert built.status_code == 200, built.text
    body = built.json()
    assert body["views"] == 100.0
    assert body["engagement_rate"] > 0

    fetched = client.get(
        f"/performance-snapshots/{body['snapshot_id']}",
        params={"org_id": "org-1"},
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["snapshot_id"] == body["snapshot_id"]

    wrong_org = client.get(
        f"/performance-snapshots/{body['snapshot_id']}",
        params={"org_id": "org-other"},
    )
    assert wrong_org.status_code == 404

    listing = client.get(
        "/performance-snapshots",
        params={"org_id": "org-1"},
    )
    assert len(listing.json()) == 1


def test_snapshot_missing_maps_to_404(client) -> None:
    resp = client.post(
        "/performance-snapshots",
        json={"org_id": "org-1", "platform": "youtube", "content_ref": "content://missing.mp4"},
    )
    assert resp.status_code == 404


def test_snapshot_conflict_maps_to_409(engine: Engine, client) -> None:
    client.post(
        "/analytics/collect",
        json={
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt-1",
            "content_ref": "content://conflict.mp4",
            "external_id": "ext-c",
        },
    )
    first = client.post(
        "/performance-snapshots",
        json={
            "org_id": "org-1",
            "platform": "youtube",
            "content_ref": "content://conflict.mp4",
        },
    )
    assert first.status_code == 200, first.text

    # Mutate the stored VIEW metric so the re-derivation differs from the
    # recorded snapshot -> conflict (409), not a silent overwrite.
    import sqlalchemy as sa

    from clipos_api.analytics import AnalyticsMetricRecord

    with engine.begin() as conn:
        conn.execute(
            sa.update(AnalyticsMetricRecord)
            .where(
                AnalyticsMetricRecord.content_ref == "content://conflict.mp4",
                AnalyticsMetricRecord.kind == "VIEW",
            )
            .values(value=9999.0)
        )

    resp = client.post(
        "/performance-snapshots",
        json={
            "org_id": "org-1",
            "platform": "youtube",
            "content_ref": "content://conflict.mp4",
        },
    )
    assert resp.status_code == 409
