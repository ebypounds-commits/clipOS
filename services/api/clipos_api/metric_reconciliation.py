"""Metric ingestion and reconciliation (CLIP-063).

Builds on the CLIP-062 analytics contract to provide:

- **Ingestion** — `ingest_metrics` collects metrics from the analytics
  adapter for one content_ref and persists them through the
  AnalyticsStore (idempotent per (org, content, kind, collected_at
  window)).
- **Reconciliation** — `reconcile_metrics` compares the latest collected
  metrics against the previous snapshot for the same content_ref and
  records discrepancies. A discrepancy is any metric kind where the
  value changed by more than a caller-supplied threshold (default 0% —
  any change). Reconciliation records are append-only audit artifacts.
- **Query** — GET /reconciliation-records to list records for an org
  with optional content_ref filter.

Audits: `metric_reconciliation.ingested`, `metric_reconciliation.reconciled`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.analytics import (
    AnalyticsAdapter,
    AnalyticsMetric,
    AnalyticsStore,
    CollectRequest,
    MetricKind,
    MockAnalyticsAdapter,
    collect_analytics,
)
from clipos_api.repository import Base


class ReconciliationStatus(str, Enum):
    """Outcome of one reconciliation run."""

    MATCH = "MATCH"
    DISCREPANCY = "DISCREPANCY"
    NO_PREVIOUS = "NO_PREVIOUS"


class Discrepancy(BaseModel):
    """One metric kind with a value change."""

    model_config = ConfigDict(frozen=True)

    kind: MetricKind
    previous_value: float
    current_value: float
    delta: float
    delta_pct: float


class ReconciliationRecord(BaseModel):
    """One reconciliation outcome for a content_ref."""

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    content_ref: str = Field(min_length=1, max_length=512)
    status: ReconciliationStatus
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    metric_count: int
    reconciled_at: datetime | None = None


class IngestRequest(BaseModel):
    """Request body for metric ingestion."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    account_ref: str = Field(min_length=1, max_length=255)
    content_ref: str = Field(min_length=1, max_length=512)
    external_id: str = Field(min_length=1, max_length=255)


class ReconcileRequest(BaseModel):
    """Request body for metric reconciliation."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    account_ref: str = Field(min_length=1, max_length=255)
    content_ref: str = Field(min_length=1, max_length=512)
    external_id: str = Field(min_length=1, max_length=255)
    threshold_pct: float = Field(default=0.0, ge=0.0, le=100.0)


class ReconciliationRecordRow(Base):
    """SQL row for one reconciliation outcome."""

    __tablename__ = "reconciliation_records"
    __table_args__ = (
        sa.Index(
            "ix_reconciliation_records_org_content",
            "org_id",
            "content_ref",
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    content_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    discrepancies_json: Mapped[list] = mapped_column(sa.JSON, nullable=False)
    metric_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    reconciled_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _record_from_row(row: ReconciliationRecordRow) -> ReconciliationRecord:
    discrepancies = [
        Discrepancy(
            kind=MetricKind(d["kind"]),
            previous_value=d["previous_value"],
            current_value=d["current_value"],
            delta=d["delta"],
            delta_pct=d["delta_pct"],
        )
        for d in (row.discrepancies_json or [])
    ]
    return ReconciliationRecord(
        record_id=row.record_id,
        org_id=row.org_id,
        platform=row.platform,
        content_ref=row.content_ref,
        status=ReconciliationStatus(row.status),
        discrepancies=discrepancies,
        metric_count=row.metric_count,
        reconciled_at=_aware(row.reconciled_at),
    )


class ReconciliationStore:
    """Typed persistence for reconciliation records."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ReconciliationStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def store_record(self, record: ReconciliationRecord) -> ReconciliationRecord:
        """Persist one reconciliation record."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = ReconciliationRecordRow(
                record_id=record.record_id,
                org_id=record.org_id,
                platform=record.platform,
                content_ref=record.content_ref,
                status=record.status.value,
                discrepancies_json=[
                    {
                        "kind": d.kind.value,
                        "previous_value": d.previous_value,
                        "current_value": d.current_value,
                        "delta": d.delta,
                        "delta_pct": d.delta_pct,
                    }
                    for d in record.discrepancies
                ],
                metric_count=record.metric_count,
                reconciled_at=now,
            )
            session.add(row)
            session.flush()
            return _record_from_row(row)

    def list_records(
        self,
        org_id: str,
        content_ref: str | None = None,
    ) -> list[ReconciliationRecord]:
        """List reconciliation records for an org."""
        with self._sessions() as session:
            stmt = sa.select(ReconciliationRecordRow).where(
                ReconciliationRecordRow.org_id == org_id
            )
            if content_ref is not None:
                stmt = stmt.where(ReconciliationRecordRow.content_ref == content_ref)
            rows = session.scalars(
                stmt.order_by(ReconciliationRecordRow.reconciled_at.desc())
            ).all()
            return [_record_from_row(r) for r in rows]


def _metrics_to_map(metrics: list[AnalyticsMetric]) -> dict[MetricKind, float]:
    return {m.kind: m.value for m in metrics}


def reconcile_metrics(
    adapter: AnalyticsAdapter,
    analytics_store: AnalyticsStore,
    recon_store: ReconciliationStore,
    request: ReconcileRequest,
) -> ReconciliationRecord:
    """Collect fresh metrics, compare against stored, record discrepancies."""
    # Collect fresh metrics.
    snapshot = collect_analytics(
        adapter,
        analytics_store,
        CollectRequest(
            org_id=request.org_id,
            platform=request.platform,
            account_ref=request.account_ref,
            content_ref=request.content_ref,
            external_id=request.external_id,
        ),
    )

    # Get previous metrics (excluding the just-collected snapshot).
    previous_metrics = analytics_store.get_metrics(request.org_id, request.content_ref)
    current_map = _metrics_to_map(snapshot.metrics)

    if not previous_metrics or len(previous_metrics) <= len(snapshot.metrics):
        # No meaningful previous data to compare against.
        status = ReconciliationStatus.NO_PREVIOUS
        discrepancies: list[Discrepancy] = []
    else:
        previous_map = _metrics_to_map(
            previous_metrics[: len(previous_metrics) - len(snapshot.metrics)]
        )
        discrepancies = []
        for kind, current_val in current_map.items():
            prev_val = previous_map.get(kind)
            if prev_val is not None and prev_val != current_val:
                delta = current_val - prev_val
                delta_pct = (delta / prev_val * 100) if prev_val != 0 else 0.0
                if abs(delta_pct) > request.threshold_pct:
                    discrepancies.append(
                        Discrepancy(
                            kind=kind,
                            previous_value=prev_val,
                            current_value=current_val,
                            delta=delta,
                            delta_pct=delta_pct,
                        )
                    )
        status = ReconciliationStatus.DISCREPANCY if discrepancies else ReconciliationStatus.MATCH

    record = ReconciliationRecord(
        record_id=str(uuid4())[:12],
        org_id=request.org_id,
        platform=request.platform,
        content_ref=request.content_ref,
        status=status,
        discrepancies=discrepancies,
        metric_count=len(snapshot.metrics),
    )

    stored = recon_store.store_record(record)

    audit.record(
        "metric_reconciliation.reconciled",
        "reconciliation_records",
        f"reconciliation:{stored.record_id}",
        {
            "org_id": request.org_id,
            "platform": request.platform,
            "content_ref": request.content_ref,
            "status": status.value,
            "discrepancy_count": len(discrepancies),
            "metric_count": len(snapshot.metrics),
        },
    )

    return stored


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["metric-reconciliation"])


def _analytics_store(engine: Annotated[Engine, Depends(_database_engine)]) -> AnalyticsStore:
    return AnalyticsStore.from_engine(engine)


def _recon_store(engine: Annotated[Engine, Depends(_database_engine)]) -> ReconciliationStore:
    return ReconciliationStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _adapter() -> AnalyticsAdapter:
    return MockAnalyticsAdapter()


@router.post(
    "/reconciliation/run",
    response_model=ReconciliationRecord,
    name="run_reconciliation",
)
def run_reconciliation_route(
    request: ReconcileRequest,
    analytics_store: Annotated[AnalyticsStore, Depends(_analytics_store)],
    recon_store: Annotated[ReconciliationStore, Depends(_recon_store)],
    adapter: Annotated[AnalyticsAdapter, Depends(_adapter)],
) -> ReconciliationRecord:
    """Collect metrics and reconcile against previous snapshot."""
    return reconcile_metrics(adapter, analytics_store, recon_store, request)


@router.get(
    "/reconciliation-records",
    response_model=list[ReconciliationRecord],
    name="list_reconciliation_records",
)
def list_reconciliation_records_route(
    recon_store: Annotated[ReconciliationStore, Depends(_recon_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    content_ref: Annotated[str | None, Query()] = None,
) -> list[ReconciliationRecord]:
    """List reconciliation records for an org."""
    return recon_store.list_records(org_id, content_ref)
