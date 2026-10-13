"""Tests for analytics dashboards (CLIP-065)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.analytics_dashboards import summarize_snapshots
from clipos_api.main import app
from clipos_api.performance_snapshots import PerformanceSnapshot


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-065-dashboard-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


def _snapshot(
    content_ref: str,
    views: float,
    engagement_rate: float,
    likes: float = 10.0,
    comments: float = 5.0,
    shares: float = 5.0,
    platform: str = "youtube",
    snapshot_id: str = "snap-1",
) -> PerformanceSnapshot:
    return PerformanceSnapshot(
        snapshot_id=snapshot_id,
        org_id="org-1",
        platform=platform,
        content_ref=content_ref,
        views=views,
        likes=likes,
        comments=comments,
        shares=shares,
        impressions=views * 10,
        watch_time_minutes=60.0,
        click_throughs=1.0,
        engagement_rate=engagement_rate,
    )


# ---------------------------------------------------------------------------
# Pure summary
# ---------------------------------------------------------------------------


def test_summarize_empty() -> None:
    summary = summarize_snapshots("org-1", [])
    assert summary.content_count == 0
    assert summary.total_views == 0.0
    assert summary.avg_engagement_rate == 0.0
    assert summary.top_content == []


def test_summarize_totals_and_average() -> None:
    snapshots = [
        _snapshot(
            "content://a.mp4", views=100, engagement_rate=10.0, likes=8, comments=2, shares=0
        ),
        _snapshot(
            "content://b.mp4", views=300, engagement_rate=30.0, likes=24, comments=6, shares=0
        ),
    ]
    summary = summarize_snapshots("org-1", snapshots)
    assert summary.content_count == 2
    assert summary.total_views == 400.0
    assert summary.total_likes == 32.0
    assert summary.total_comments == 8.0
    assert summary.avg_engagement_rate == pytest.approx(20.0)


def test_summarize_top_content_ranked_and_limited() -> None:
    snapshots = [
        _snapshot(f"content://v{i}.mp4", views=100, engagement_rate=float(i), snapshot_id=f"s{i}")
        for i in range(8)
    ]
    summary = summarize_snapshots("org-1", snapshots)
    assert len(summary.top_content) == 5
    rates = [t.engagement_rate for t in summary.top_content]
    assert rates == sorted(rates, reverse=True)
    assert summary.top_content[0].content_ref == "content://v7.mp4"


def test_summarize_tie_break_deterministic() -> None:
    a = _snapshot("content://a.mp4", views=100, engagement_rate=10.0)
    b = _snapshot("content://b.mp4", views=100, engagement_rate=10.0)
    summary_one = summarize_snapshots("org-1", [a, b])
    summary_two = summarize_snapshots("org-1", [b, a])
    assert [t.content_ref for t in summary_one.top_content] == [
        t.content_ref for t in summary_two.top_content
    ]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/analytics/dashboard" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_dashboard_empty_org(client) -> None:
    resp = client.get("/analytics/dashboard", params={"org_id": "org-empty"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content_count"] == 0
    assert body["top_content"] == []


def test_dashboard_with_seeded_snapshots(engine: Engine, client) -> None:
    from clipos_api.performance_snapshots import PerformanceSnapshotStore

    store = PerformanceSnapshotStore.from_engine(engine)
    store.record_snapshot(
        "org-1",
        "youtube",
        "content://a.mp4",
        {
            "views": 100.0,
            "likes": 10.0,
            "comments": 5.0,
            "shares": 5.0,
            "impressions": 1000.0,
            "watch_time_minutes": 42.0,
            "click_throughs": 3.0,
            "engagement_rate": 20.0,
        },
        "snap-a",
    )
    resp = client.get("/analytics/dashboard", params={"org_id": "org-1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content_count"] == 1
    assert body["total_views"] == 100.0
    assert body["avg_engagement_rate"] == 20.0
    assert body["top_content"][0]["content_ref"] == "content://a.mp4"

    other = client.get("/analytics/dashboard", params={"org_id": "org-other"})
    assert other.json()["content_count"] == 0
