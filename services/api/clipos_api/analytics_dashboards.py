"""Analytics dashboards (CLIP-065).

Org-level roll-up over the CLIP-064 performance snapshots (PRD §H
"normalized metrics"):

- **Pure summary** — `summarize_snapshots` derives DashboardSummary from
  stored snapshots: per-kind totals, content count, average engagement
  rate, and the top content rows by engagement (ties broken by views,
  then content_ref — deterministic). Missing/zero snapshots yield a
  zeroed summary, never a division error.
- **Query** — GET /analytics/dashboard?org_id= returns the summary.

No LLM, no network, no provider identifier, no new table.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine

from clipos_api.performance_snapshots import (
    PerformanceSnapshot,
    PerformanceSnapshotStore,
)

TOP_CONTENT_LIMIT = 5


class TopContent(BaseModel):
    """One content_ref ranked in the dashboard top list."""

    model_config = ConfigDict(frozen=True)

    content_ref: str = Field(min_length=1, max_length=512)
    platform: str = Field(min_length=1, max_length=64)
    views: float
    engagement_rate: float


class DashboardSummary(BaseModel):
    """Normalized org-level analytics roll-up."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    content_count: int = Field(ge=0)
    total_views: float = Field(ge=0)
    total_likes: float = Field(ge=0)
    total_comments: float = Field(ge=0)
    total_shares: float = Field(ge=0)
    avg_engagement_rate: float = Field(ge=0)
    top_content: list[TopContent] = Field(default_factory=list)


def summarize_snapshots(
    org_id: str,
    snapshots: list[PerformanceSnapshot],
) -> DashboardSummary:
    """Pure roll-up: totals, average engagement, top content by engagement."""
    if not snapshots:
        return DashboardSummary(
            org_id=org_id,
            content_count=0,
            total_views=0.0,
            total_likes=0.0,
            total_comments=0.0,
            total_shares=0.0,
            avg_engagement_rate=0.0,
            top_content=[],
        )
    total_views = sum(s.views for s in snapshots)
    avg_engagement = sum(s.engagement_rate for s in snapshots) / len(snapshots)
    ranked = sorted(
        snapshots,
        key=lambda s: (s.engagement_rate, s.views, s.content_ref),
        reverse=True,
    )
    top = [
        TopContent(
            content_ref=s.content_ref,
            platform=s.platform,
            views=s.views,
            engagement_rate=s.engagement_rate,
        )
        for s in ranked[:TOP_CONTENT_LIMIT]
    ]
    return DashboardSummary(
        org_id=org_id,
        content_count=len(snapshots),
        total_views=total_views,
        total_likes=sum(s.likes for s in snapshots),
        total_comments=sum(s.comments for s in snapshots),
        total_shares=sum(s.shares for s in snapshots),
        avg_engagement_rate=avg_engagement,
        top_content=top,
    )


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["analytics"])


def _snapshot_store(
    engine: Annotated[Engine, Depends(_database_engine)],
) -> PerformanceSnapshotStore:
    return PerformanceSnapshotStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


@router.get(
    "/analytics/dashboard",
    response_model=DashboardSummary,
    name="get_analytics_dashboard",
)
def get_analytics_dashboard_route(
    snapshot_store: Annotated[PerformanceSnapshotStore, Depends(_snapshot_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> DashboardSummary:
    """Org-level roll-up over stored performance snapshots."""
    snapshots = snapshot_store.list_snapshots(org_id)
    return summarize_snapshots(org_id, snapshots)
