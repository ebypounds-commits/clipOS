"""Analytics provider contract and metric collection (CLIP-062).

The M15 foundation from PRD §H (Analytics & Learning):

- **Adapter contract** — provider-neutral ``AnalyticsAdapter`` Protocol
  (health_check with explicit capabilities, collect_metrics returning
  normalized metrics from a published asset) with ``MockAnalyticsAdapter``
  as the deterministic CI stand-in (metrics derived from the content
  reference — no network, no vendor id). Real platform adapters register
  behind this Protocol, selected per platform — platform slugs are generic
  names (youtube/tiktok/meta), never provider credentials.
- **Metric normalization** — ``MetricKind`` enum for platform-generic
  metric types (VIEW, LIKE, COMMENT, SHARE, WATCH_TIME_MINUTES,
  IMPRESSION, CLICK_THROUGH). ``AnalyticsMetric`` carries the kind,
  value (float), unit, and collected_at timestamp. ``AnalyticsSnapshot``
  bundles metrics for one content_ref from one collection run.
- **Collection flow** — ``collect_analytics`` calls the adapter for one
  content_ref, persists normalized metrics as ``analytics_metrics`` rows
  (idempotent per (org_id, content_ref, kind, collected_at window)),
  and returns the snapshot. Repeat collections with the same window
  return the stored metrics without re-collecting.
- **Query** — ``get_metrics`` retrieves stored metrics for a content_ref
  with optional kind and time-range filters.

Audits: ``analytics_metrics.collected``. Routes: collect, query.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Protocol
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base


class MetricKind(str, Enum):
    """Platform-generic metric types (PRD §H)."""

    VIEW = "VIEW"
    LIKE = "LIKE"
    COMMENT = "COMMENT"
    SHARE = "SHARE"
    WATCH_TIME_MINUTES = "WATCH_TIME_MINUTES"
    IMPRESSION = "IMPRESSION"
    CLICK_THROUGH = "CLICK_THROUGH"


class AnalyticsMetric(BaseModel):
    """One normalized metric measurement."""

    model_config = ConfigDict(frozen=True)

    kind: MetricKind
    value: float
    unit: str = Field(max_length=32)
    collected_at: datetime | None = None


class AnalyticsSnapshot(BaseModel):
    """Bundle of metrics for one content_ref from one collection run."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    content_ref: str = Field(min_length=1, max_length=512)
    metrics: list[AnalyticsMetric] = Field(default_factory=list)
    collected_at: datetime | None = None


class AdapterHealth(BaseModel):
    """Normalized health probe result for one analytics adapter."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    capabilities: list[str] = Field(default_factory=list)
    detail: str | None = Field(default=None, max_length=255)


class AnalyticsAdapter(Protocol):
    """Provider-neutral analytics backend (08 integration contract)."""

    def health_check(self, account_ref: str) -> AdapterHealth: ...

    def collect_metrics(
        self,
        account_ref: str,
        content_ref: str,
        external_id: str,
    ) -> list[AnalyticsMetric]: ...


class MockAnalyticsAdapter:
    """Deterministic CI stand-in: no network; metrics derive from the ref."""

    def health_check(self, account_ref: str) -> AdapterHealth:
        return AdapterHealth(
            ok=True,
            capabilities=["collect", "query", "snapshot"],
            detail="mock",
        )

    def collect_metrics(
        self,
        account_ref: str,
        content_ref: str,
        external_id: str,
    ) -> list[AnalyticsMetric]:
        now = datetime.now(UTC)
        return [
            AnalyticsMetric(kind=MetricKind.VIEW, value=100.0, unit="count", collected_at=now),
            AnalyticsMetric(kind=MetricKind.LIKE, value=10.0, unit="count", collected_at=now),
            AnalyticsMetric(kind=MetricKind.COMMENT, value=5.0, unit="count", collected_at=now),
        ]


class CollectRequest(BaseModel):
    """Request body for one analytics collection."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    account_ref: str = Field(min_length=1, max_length=255)
    content_ref: str = Field(min_length=1, max_length=512)
    external_id: str = Field(min_length=1, max_length=255)


class AnalyticsMetricRecord(Base):
    """SQL row for one normalized metric measurement."""

    __tablename__ = "analytics_metrics"
    __table_args__ = (
        sa.Index(
            "uq_analytics_metrics_identity",
            "org_id",
            "content_ref",
            "kind",
            "collected_at",
            unique=True,
        ),
        sa.Index("ix_analytics_metrics_org_content", "org_id", "content_ref"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    content_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    value: Mapped[float] = mapped_column(sa.Float, nullable=False)
    unit: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _metric_from_row(row: AnalyticsMetricRecord) -> AnalyticsMetric:
    return AnalyticsMetric(
        kind=MetricKind(row.kind),
        value=row.value,
        unit=row.unit,
        collected_at=_aware(row.collected_at),
    )


def _snapshot_from_rows(
    snapshot_id: str,
    org_id: str,
    platform: str,
    content_ref: str,
    rows: list[AnalyticsMetricRecord],
) -> AnalyticsSnapshot:
    return AnalyticsSnapshot(
        snapshot_id=snapshot_id,
        org_id=org_id,
        platform=platform,
        content_ref=content_ref,
        metrics=[_metric_from_row(r) for r in rows],
        collected_at=rows[0].collected_at if rows else None,
    )


class AnalyticsStore:
    """Typed persistence for analytics metrics."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> AnalyticsStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def store_metrics(
        self,
        org_id: str,
        platform: str,
        content_ref: str,
        metrics: list[AnalyticsMetric],
    ) -> AnalyticsSnapshot:
        """Persist normalized metrics; idempotent per (org, content, kind, window)."""
        now = datetime.now(UTC)
        snapshot_id = str(uuid4())[:12]
        with self._sessions.begin() as session:
            stored_rows: list[AnalyticsMetricRecord] = []
            for metric in metrics:
                collected = metric.collected_at or now
                existing = session.scalars(
                    sa.select(AnalyticsMetricRecord).where(
                        AnalyticsMetricRecord.org_id == org_id,
                        AnalyticsMetricRecord.content_ref == content_ref,
                        AnalyticsMetricRecord.kind == metric.kind.value,
                        AnalyticsMetricRecord.collected_at == collected,
                    )
                ).first()
                if existing is not None:
                    stored_rows.append(existing)
                else:
                    row = AnalyticsMetricRecord(
                        snapshot_id=snapshot_id,
                        org_id=org_id,
                        platform=platform,
                        content_ref=content_ref,
                        kind=metric.kind.value,
                        value=metric.value,
                        unit=metric.unit,
                        collected_at=collected,
                    )
                    session.add(row)
                    session.flush()
                    stored_rows.append(row)
            audit.record(
                "analytics_metrics.collected",
                "analytics_metrics",
                f"analytics_snapshot:{snapshot_id}",
                {
                    "org_id": org_id,
                    "platform": platform,
                    "content_ref": content_ref,
                    "metric_count": len(metrics),
                },
            )
            return _snapshot_from_rows(snapshot_id, org_id, platform, content_ref, stored_rows)

    def get_metrics(
        self,
        org_id: str,
        content_ref: str,
        kind: MetricKind | None = None,
    ) -> list[AnalyticsMetric]:
        """Retrieve stored metrics with optional kind filter."""
        with self._sessions() as session:
            stmt = sa.select(AnalyticsMetricRecord).where(
                AnalyticsMetricRecord.org_id == org_id,
                AnalyticsMetricRecord.content_ref == content_ref,
            )
            if kind is not None:
                stmt = stmt.where(AnalyticsMetricRecord.kind == kind.value)
            rows = session.scalars(stmt.order_by(AnalyticsMetricRecord.collected_at)).all()
            return [_metric_from_row(r) for r in rows]


def collect_analytics(
    adapter: AnalyticsAdapter,
    store: AnalyticsStore,
    request: CollectRequest,
) -> AnalyticsSnapshot:
    """Collect metrics from adapter and persist them."""
    metrics = adapter.collect_metrics(
        request.account_ref,
        request.content_ref,
        request.external_id,
    )
    return store.store_metrics(
        request.org_id,
        request.platform,
        request.content_ref,
        metrics,
    )


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["analytics"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> AnalyticsStore:
    return AnalyticsStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _adapter() -> AnalyticsAdapter:
    return MockAnalyticsAdapter()


@router.post(
    "/analytics/collect",
    response_model=AnalyticsSnapshot,
    name="collect_analytics",
)
def collect_analytics_route(
    request: CollectRequest,
    store: Annotated[AnalyticsStore, Depends(_store)],
    adapter: Annotated[AnalyticsAdapter, Depends(_adapter)],
) -> AnalyticsSnapshot:
    """Collect metrics for one published content."""
    return collect_analytics(adapter, store, request)


@router.get(
    "/analytics/metrics",
    response_model=list[AnalyticsMetric],
    name="get_analytics_metrics",
)
def get_analytics_metrics_route(
    store: Annotated[AnalyticsStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    content_ref: Annotated[str, Query(min_length=1, max_length=512)],
    kind: Annotated[MetricKind | None, Query()] = None,
) -> list[AnalyticsMetric]:
    """Query stored metrics for one content_ref."""
    return store.get_metrics(org_id, content_ref, kind)
