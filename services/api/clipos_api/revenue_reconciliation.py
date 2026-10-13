"""Revenue dashboards and reconciliation workflows (CLIP-073).

Closes the M17 monetization loop over the CLIP-070 ledger (PRD §I +
journey 4 "economics calculated"):

- **Dashboard** — `summarize_revenue` is a pure roll-up over stored
  ledger entries: totals per currency (never mixed), entry counts per
  RevenueKind, and a per-source total (same per-currency discipline).
  GET /revenue/dashboard?org_id= returns it; optional period filters
  restrict the underlying entries to an exact (period_start,
  period_end) window.
- **Reconciliation** — `reconcile_revenue` is pure: compare recorded
  ledger cents for one (source, currency, window) against an expected
  total the operator supplies from an external statement. MATCH when
  |delta_pct| <= tolerance_pct (default 0.0 — exact); otherwise
  DISCREPANCY. delta_pct is relative to expected (0 unless both sides
  are 0). `run_reconciliation` sums the ledger for the exact window +
  currency, evaluates, and persists a `revenue_reconciliations` row
  (migration 0051, unique (org, reconciliation_id), source existence
  404). Audited `revenue_reconciliations.recorded`.

Routes (CLIP-033 pattern): GET /revenue/dashboard, POST
/revenue-reconciliations, GET /revenue-reconciliations?org_id=.
No LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base
from clipos_api.revenue_ledger import (
    CURRENCY_PATTERN,
    RevenueEntry,
    RevenueEntryRecord,
    RevenueKind,
    RevenueSourceRecord,
)

DEFAULT_TOLERANCE_PCT = 0.0


class ReconciliationStatus(str, Enum):
    """Outcome of one revenue reconciliation."""

    MATCH = "MATCH"
    DISCREPANCY = "DISCREPANCY"


class ReconcileResult(BaseModel):
    """Pure output of recorded-vs-expected comparison."""

    model_config = ConfigDict(frozen=True)

    status: ReconciliationStatus
    recorded_cents: int
    expected_cents: int
    delta_cents: int
    delta_pct: float
    tolerance_pct: float


class RevenueDashboard(BaseModel):
    """Pure org-level revenue roll-up."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    entry_count: int = Field(ge=0)
    total_cents_by_currency: dict[str, int] = Field(default_factory=dict)
    total_cents_by_kind: dict[str, int] = Field(default_factory=dict)
    total_cents_by_source: dict[str, int] = Field(default_factory=dict)


class RunReconciliationRequest(BaseModel):
    """Request body for one reconciliation run."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    reconciliation_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    source_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    period_start: date
    period_end: date
    expected_cents: int = Field(ge=0, le=1_000_000_000_000)
    tolerance_pct: float = Field(default=DEFAULT_TOLERANCE_PCT, ge=0.0, le=100.0)
    actor_ref: str = Field(min_length=1, max_length=255)


class RevenueReconciliation(BaseModel):
    """One stored reconciliation record."""

    model_config = ConfigDict(frozen=True)

    reconciliation_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    period_start: date
    period_end: date
    status: ReconciliationStatus
    recorded_cents: int
    expected_cents: int
    delta_cents: int
    delta_pct: float
    tolerance_pct: float
    actor_ref: str = Field(min_length=1, max_length=255)
    created_at: datetime | None = None


def reconcile_revenue(
    recorded_cents: int,
    expected_cents: int,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
) -> ReconcileResult:
    """Pure recorded-vs-expected comparison.

    delta_pct is relative to expected_cents; zero expected reports 0%
    only when recorded is also 0, otherwise 100% (full miss).
    MATCH requires |delta_pct| <= tolerance_pct.
    """
    delta = recorded_cents - expected_cents
    if expected_cents == 0:
        delta_pct = 0.0 if delta == 0 else 100.0
    else:
        delta_pct = delta / expected_cents * 100.0
    status = (
        ReconciliationStatus.MATCH
        if abs(delta_pct) <= tolerance_pct
        else ReconciliationStatus.DISCREPANCY
    )
    return ReconcileResult(
        status=status,
        recorded_cents=recorded_cents,
        expected_cents=expected_cents,
        delta_cents=delta,
        delta_pct=delta_pct,
        tolerance_pct=tolerance_pct,
    )


def summarize_revenue(
    org_id: str,
    entries: list[RevenueEntry],
) -> RevenueDashboard:
    """Pure roll-up: totals per currency/kind/source (never mixed)."""
    by_currency: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for entry in entries:
        by_currency[entry.currency] = by_currency.get(entry.currency, 0) + entry.amount_cents
        kind = entry.revenue_kind.value
        # Kind totals are per-currency keyed to keep currencies unmixed.
        by_kind[f"{kind}:{entry.currency}"] = (
            by_kind.get(f"{kind}:{entry.currency}", 0) + entry.amount_cents
        )
        by_source[f"{entry.source_id}:{entry.currency}"] = (
            by_source.get(f"{entry.source_id}:{entry.currency}", 0) + entry.amount_cents
        )
    return RevenueDashboard(
        org_id=org_id,
        entry_count=len(entries),
        total_cents_by_currency=by_currency,
        total_cents_by_kind=by_kind,
        total_cents_by_source=by_source,
    )


class RevenueReconciliationRecord(Base):
    """SQL row for one reconciliation run."""

    __tablename__ = "revenue_reconciliations"
    __table_args__ = (
        sa.Index(
            "uq_revenue_reconciliations_identity",
            "org_id",
            "reconciliation_id",
            unique=True,
        ),
        sa.Index("ix_revenue_reconciliations_org_source", "org_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    reconciliation_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    period_start: Mapped[date] = mapped_column(sa.Date, nullable=False)
    period_end: Mapped[date] = mapped_column(sa.Date, nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    recorded_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    expected_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    delta_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    delta_pct: Mapped[float] = mapped_column(sa.Float, nullable=False)
    tolerance_pct: Mapped[float] = mapped_column(sa.Float, nullable=False)
    actor_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _reconciliation_from_row(row: RevenueReconciliationRecord) -> RevenueReconciliation:
    return RevenueReconciliation(
        reconciliation_id=row.reconciliation_id,
        org_id=row.org_id,
        source_id=row.source_id,
        currency=row.currency,
        period_start=row.period_start,
        period_end=row.period_end,
        status=ReconciliationStatus(row.status),
        recorded_cents=row.recorded_cents,
        expected_cents=row.expected_cents,
        delta_cents=row.delta_cents,
        delta_pct=row.delta_pct,
        tolerance_pct=row.tolerance_pct,
        actor_ref=row.actor_ref,
        created_at=_aware(row.created_at),
    )


class RevenueDashboardStore:
    """Typed persistence for the dashboard and reconciliation records."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> RevenueDashboardStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def list_entries(
        self,
        org_id: str,
        period_start: date | None = None,
        period_end: date | None = None,
    ) -> list[RevenueEntry]:
        """Ledger entries, optionally restricted to an exact window."""
        with self._sessions() as session:
            stmt = sa.select(RevenueEntryRecord).where(RevenueEntryRecord.org_id == org_id)
            if period_start is not None:
                stmt = stmt.where(RevenueEntryRecord.period_start == period_start)
            if period_end is not None:
                stmt = stmt.where(RevenueEntryRecord.period_end == period_end)
            rows = session.scalars(
                stmt.order_by(RevenueEntryRecord.created_at, RevenueEntryRecord.id)
            ).all()
            return [
                RevenueEntry(
                    entry_id=r.entry_id,
                    org_id=r.org_id,
                    source_id=r.source_id,
                    external_ref=r.external_ref,
                    revenue_kind=RevenueKind(r.revenue_kind),
                    amount_cents=r.amount_cents,
                    currency=r.currency,
                    period_start=r.period_start,
                    period_end=r.period_end,
                    created_at=_aware(r.created_at),
                )
                for r in rows
            ]

    def dashboard(
        self,
        org_id: str,
        period_start: date | None = None,
        period_end: date | None = None,
    ) -> RevenueDashboard:
        return summarize_revenue(org_id, self.list_entries(org_id, period_start, period_end))

    def run_reconciliation(self, request: RunReconciliationRequest) -> RevenueReconciliation:
        """Sum the ledger for the exact window+currency and reconcile.

        Source must exist (404); identical (org, reconciliation_id)
        with changed inputs conflicts (409).
        """
        now = datetime.now(UTC)
        if request.period_end < request.period_start:
            raise ValueError("period_end must not precede period_start")
        with self._sessions.begin() as session:
            source = session.scalars(
                sa.select(RevenueSourceRecord).where(
                    RevenueSourceRecord.org_id == request.org_id,
                    RevenueSourceRecord.source_id == request.source_id,
                )
            ).first()
            if source is None:
                raise ValueError(f"revenue source not found: {request.source_id}")
            existing = session.scalars(
                sa.select(RevenueReconciliationRecord).where(
                    RevenueReconciliationRecord.org_id == request.org_id,
                    RevenueReconciliationRecord.reconciliation_id == request.reconciliation_id,
                )
            ).first()
            if existing is not None:
                stored = _reconciliation_from_row(existing)
                if (
                    stored.source_id != request.source_id
                    or stored.currency != request.currency
                    or stored.period_start != request.period_start
                    or stored.period_end != request.period_end
                    or stored.expected_cents != request.expected_cents
                    or stored.tolerance_pct != request.tolerance_pct
                ):
                    raise ValueError(
                        f"reconciliation already recorded with different fields: "
                        f"{request.reconciliation_id}"
                    )
                return stored
            recorded_rows = session.scalars(
                sa.select(RevenueEntryRecord).where(
                    RevenueEntryRecord.org_id == request.org_id,
                    RevenueEntryRecord.source_id == request.source_id,
                    RevenueEntryRecord.currency == request.currency,
                    RevenueEntryRecord.period_start == request.period_start,
                    RevenueEntryRecord.period_end == request.period_end,
                )
            ).all()
        recorded_cents = sum(r.amount_cents for r in recorded_rows)
        result = reconcile_revenue(recorded_cents, request.expected_cents, request.tolerance_pct)
        with self._sessions.begin() as session:
            raced = session.scalars(
                sa.select(RevenueReconciliationRecord).where(
                    RevenueReconciliationRecord.org_id == request.org_id,
                    RevenueReconciliationRecord.reconciliation_id == request.reconciliation_id,
                )
            ).first()
            if raced is not None:
                return _reconciliation_from_row(raced)
            row = RevenueReconciliationRecord(
                reconciliation_id=request.reconciliation_id,
                org_id=request.org_id,
                source_id=request.source_id,
                currency=request.currency,
                period_start=request.period_start,
                period_end=request.period_end,
                status=result.status.value,
                recorded_cents=result.recorded_cents,
                expected_cents=result.expected_cents,
                delta_cents=result.delta_cents,
                delta_pct=result.delta_pct,
                tolerance_pct=result.tolerance_pct,
                actor_ref=request.actor_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "revenue_reconciliations.recorded",
                "revenue_reconciliations",
                f"revenue_reconciliation:{request.reconciliation_id}",
                {
                    "org_id": request.org_id,
                    "source_id": request.source_id,
                    "status": result.status.value,
                    "delta_cents": result.delta_cents,
                },
            )
            return _reconciliation_from_row(row)

    def list_reconciliations(
        self, org_id: str, source_id: str | None = None
    ) -> list[RevenueReconciliation]:
        with self._sessions() as session:
            stmt = sa.select(RevenueReconciliationRecord).where(
                RevenueReconciliationRecord.org_id == org_id
            )
            if source_id is not None:
                stmt = stmt.where(RevenueReconciliationRecord.source_id == source_id)
            rows = session.scalars(
                stmt.order_by(
                    RevenueReconciliationRecord.created_at,
                    RevenueReconciliationRecord.id,
                )
            ).all()
            return [_reconciliation_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["revenue"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> RevenueDashboardStore:
    return RevenueDashboardStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.get("/revenue/dashboard", response_model=RevenueDashboard, name="revenue_dashboard")
def revenue_dashboard_route(
    store: Annotated[RevenueDashboardStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    period_start: Annotated[date | None, Query()] = None,
    period_end: Annotated[date | None, Query()] = None,
) -> RevenueDashboard:
    """Org-level revenue roll-up (optionally exact-window filtered)."""
    return store.dashboard(org_id, period_start, period_end)


@router.post(
    "/revenue-reconciliations",
    response_model=RevenueReconciliation,
    name="run_revenue_reconciliation",
)
def run_revenue_reconciliation_route(
    request: RunReconciliationRequest,
    store: Annotated[RevenueDashboardStore, Depends(_store)],
) -> RevenueReconciliation:
    """Compare ledger totals for one window against an expected total."""
    try:
        return store.run_reconciliation(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/revenue-reconciliations",
    response_model=list[RevenueReconciliation],
    name="list_revenue_reconciliations",
)
def list_revenue_reconciliations_route(
    store: Annotated[RevenueDashboardStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    source_id: Annotated[str | None, Query(max_length=128)] = None,
) -> list[RevenueReconciliation]:
    """List reconciliation records for one org."""
    return store.list_reconciliations(org_id, source_id)
