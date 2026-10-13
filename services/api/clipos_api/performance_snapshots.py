"""Normalized performance snapshots (CLIP-064).

Builds on the CLIP-062/063 analytics contract to produce one normalized
performance snapshot per content_ref from stored metrics (PRD §H
"normalized metrics, retention snapshots"):

- **Pure normalization** — `build_snapshot_fields` derives platform-generic
  rates from the stored metrics: engagement_rate
  = (LIKE + COMMENT + SHARE) / VIEW * 100 (0 when VIEW is absent or 0),
  plus one stored count per metric kind. No LLM, no provider identifier,
  no network.
- **Persistence** — `performance_snapshots` table (migration 0042,
  CREATE-only) unique per (org_id, content_ref); missing stored metrics
  -> 404. Audited `performance_snapshots.recorded`.

Routes (CLIP-033 pattern): POST /performance-snapshots,
GET /performance-snapshots?org_id=.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.analytics import AnalyticsStore, MetricKind
from clipos_api.repository import Base


class PerformanceSnapshot(BaseModel):
    """One normalized performance snapshot for one content_ref."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    content_ref: str = Field(min_length=1, max_length=512)
    views: float
    likes: float
    comments: float
    shares: float
    impressions: float
    watch_time_minutes: float
    click_throughs: float
    engagement_rate: float
    created_at: datetime | None = None


class SnapshotRequest(BaseModel):
    """Request body for building a performance snapshot."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    content_ref: str = Field(min_length=1, max_length=512)


class PerformanceSnapshotRecord(Base):
    """SQL row for one performance snapshot."""

    __tablename__ = "performance_snapshots"
    __table_args__ = (
        sa.Index(
            "uq_performance_snapshots_org_content",
            "org_id",
            "content_ref",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    content_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    views: Mapped[float] = mapped_column(sa.Float, nullable=False)
    likes: Mapped[float] = mapped_column(sa.Float, nullable=False)
    comments: Mapped[float] = mapped_column(sa.Float, nullable=False)
    shares: Mapped[float] = mapped_column(sa.Float, nullable=False)
    impressions: Mapped[float] = mapped_column(sa.Float, nullable=False)
    watch_time_minutes: Mapped[float] = mapped_column(sa.Float, nullable=False)
    click_throughs: Mapped[float] = mapped_column(sa.Float, nullable=False)
    engagement_rate: Mapped[float] = mapped_column(sa.Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def build_snapshot_fields(
    metrics: dict[MetricKind, float],
) -> dict[str, float]:
    """Pure metric-map -> normalized snapshot fields (PRD §H).

    Engagement rate = (LIKE + COMMENT + SHARE) / VIEW * 100; 0 when the
    view count is missing or zero. All fields default to 0 when a kind
    has no stored measurement.
    """
    views = metrics.get(MetricKind.VIEW, 0.0)
    likes = metrics.get(MetricKind.LIKE, 0.0)
    comments = metrics.get(MetricKind.COMMENT, 0.0)
    shares = metrics.get(MetricKind.SHARE, 0.0)
    engagement = likes + comments + shares
    engagement_rate = (engagement / views * 100.0) if views > 0 else 0.0
    return {
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "impressions": metrics.get(MetricKind.IMPRESSION, 0.0),
        "watch_time_minutes": metrics.get(MetricKind.WATCH_TIME_MINUTES, 0.0),
        "click_throughs": metrics.get(MetricKind.CLICK_THROUGH, 0.0),
        "engagement_rate": engagement_rate,
    }


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _snapshot_from_row(row: PerformanceSnapshotRecord) -> PerformanceSnapshot:
    return PerformanceSnapshot(
        snapshot_id=row.snapshot_id,
        org_id=row.org_id,
        platform=row.platform,
        content_ref=row.content_ref,
        views=row.views,
        likes=row.likes,
        comments=row.comments,
        shares=row.shares,
        impressions=row.impressions,
        watch_time_minutes=row.watch_time_minutes,
        click_throughs=row.click_throughs,
        engagement_rate=row.engagement_rate,
        created_at=_aware(row.created_at),
    )


class PerformanceSnapshotStore:
    """Typed persistence for performance snapshots."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> PerformanceSnapshotStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def record_snapshot(
        self,
        org_id: str,
        platform: str,
        content_ref: str,
        fields: dict[str, float],
        snapshot_id: str,
    ) -> PerformanceSnapshot:
        """Idempotent per (org, content_ref); conflict on changed fields."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(PerformanceSnapshotRecord).where(
                    PerformanceSnapshotRecord.org_id == org_id,
                    PerformanceSnapshotRecord.content_ref == content_ref,
                )
            ).first()
            if existing is not None:
                stored = _snapshot_from_row(existing)
                if (
                    stored.views != fields["views"]
                    or stored.likes != fields["likes"]
                    or stored.comments != fields["comments"]
                    or stored.shares != fields["shares"]
                    or stored.engagement_rate != fields["engagement_rate"]
                ):
                    raise ValueError(
                        f"performance snapshot already recorded with different fields: "
                        f"{content_ref}"
                    )
                return stored
            row = PerformanceSnapshotRecord(
                snapshot_id=snapshot_id,
                org_id=org_id,
                platform=platform,
                content_ref=content_ref,
                views=fields["views"],
                likes=fields["likes"],
                comments=fields["comments"],
                shares=fields["shares"],
                impressions=fields["impressions"],
                watch_time_minutes=fields["watch_time_minutes"],
                click_throughs=fields["click_throughs"],
                engagement_rate=fields["engagement_rate"],
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "performance_snapshots.recorded",
                "performance_snapshots",
                f"performance_snapshot:{snapshot_id}",
                {
                    "org_id": org_id,
                    "platform": platform,
                    "content_ref": content_ref,
                    "engagement_rate": fields["engagement_rate"],
                },
            )
            return _snapshot_from_row(row)

    def get_snapshot(self, org_id: str, content_ref: str) -> PerformanceSnapshot | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(PerformanceSnapshotRecord).where(
                    PerformanceSnapshotRecord.org_id == org_id,
                    PerformanceSnapshotRecord.content_ref == content_ref,
                )
            ).first()
            return _snapshot_from_row(row) if row is not None else None

    def get_by_snapshot_id(self, snapshot_id: str, org_id: str) -> PerformanceSnapshot | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(PerformanceSnapshotRecord).where(
                    PerformanceSnapshotRecord.snapshot_id == snapshot_id,
                    PerformanceSnapshotRecord.org_id == org_id,
                )
            ).first()
            return _snapshot_from_row(row) if row is not None else None

    def list_snapshots(self, org_id: str) -> list[PerformanceSnapshot]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(PerformanceSnapshotRecord)
                .where(PerformanceSnapshotRecord.org_id == org_id)
                .order_by(PerformanceSnapshotRecord.created_at, PerformanceSnapshotRecord.id)
            ).all()
            return [_snapshot_from_row(r) for r in rows]


def build_performance_snapshot(
    analytics_store: AnalyticsStore,
    snapshot_store: PerformanceSnapshotStore,
    request: SnapshotRequest,
) -> PerformanceSnapshot:
    """Build/persist a normalized snapshot from stored metrics.

    404-equivalent (ValueError 'not found') when the content_ref has no
    stored analytics metrics.
    """
    metrics = analytics_store.get_metrics(request.org_id, request.content_ref)
    if not metrics:
        raise ValueError(f"analytics metrics not found: {request.content_ref}")
    metric_map = {m.kind: m.value for m in metrics}
    fields = build_snapshot_fields(metric_map)
    snapshot_id = str(uuid4())[:12]
    return snapshot_store.record_snapshot(
        request.org_id, request.platform, request.content_ref, fields, snapshot_id
    )


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["performance-snapshots"])


def _analytics_store(engine: Annotated[Engine, Depends(_database_engine)]) -> AnalyticsStore:
    return AnalyticsStore.from_engine(engine)


def _snapshot_store(
    engine: Annotated[Engine, Depends(_database_engine)],
) -> PerformanceSnapshotStore:
    return PerformanceSnapshotStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


@router.post(
    "/performance-snapshots",
    response_model=PerformanceSnapshot,
    name="build_performance_snapshot",
)
def build_performance_snapshot_route(
    request: SnapshotRequest,
    analytics_store: Annotated[AnalyticsStore, Depends(_analytics_store)],
    snapshot_store: Annotated[PerformanceSnapshotStore, Depends(_snapshot_store)],
) -> PerformanceSnapshot:
    """Build a normalized performance snapshot from stored analytics metrics."""
    try:
        return build_performance_snapshot(analytics_store, snapshot_store, request)
    except ValueError as exc:
        message = str(exc)
        if "already recorded" in message:
            raise HTTPException(status_code=409, detail=message) from exc
        raise HTTPException(status_code=404, detail=message) from exc


@router.get(
    "/performance-snapshots/{snapshot_id}",
    response_model=PerformanceSnapshot,
    name="get_performance_snapshot",
)
def get_performance_snapshot_route(
    snapshot_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    snapshot_store: Annotated[PerformanceSnapshotStore, Depends(_snapshot_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> PerformanceSnapshot:
    """Get one performance snapshot by snapshot_id (org-scoped)."""
    stored = snapshot_store.get_by_snapshot_id(snapshot_id, org_id)
    if stored is None:
        raise HTTPException(
            status_code=404, detail=f"performance snapshot not found: {snapshot_id}"
        )
    return stored


@router.get(
    "/performance-snapshots",
    response_model=list[PerformanceSnapshot],
    name="list_performance_snapshots",
)
def list_performance_snapshots_route(
    snapshot_store: Annotated[PerformanceSnapshotStore, Depends(_snapshot_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[PerformanceSnapshot]:
    """List performance snapshots for one org."""
    return snapshot_store.list_snapshots(org_id)
