"""Profitability/reinvestment calculations (CLIP-076).

PRD §I profitability and reserve/reinvestment policies — pure integer
math over the CLIP-070 revenue and CLIP-074 cost ledgers:

- **Profitability** — `calculate_profitability(revenue_cents,
  cost_cents)` returns profit_cents = revenue - cost, margin_pct =
  profit / revenue * 100 (revenue 0 reports 0% when cost is also 0,
  else -100%), and `profitable` (profit >= 0). Never mixes currencies:
  both totals must already be summed for one currency.
- **Reinvestment** — `calculate_reinvestment(profit_cents,
  reinvest_pct)` splits a non-negative profit into reinvest_cents
  (floor of profit * pct / 100 — integer cents, never fractional) and
  reserve_cents = profit - reinvest. Non-positive profit produces
  zeros with reason `non_positive_profit`; reinvest_pct outside 0..100
  is rejected (422 at the route).
- **Reports** — `run_report` sums revenue_entries and cost_entries
  for one exact (period_start, period_end, currency) window, computes
  both figures, and persists a `profitability_reports` row
  (migration 0054, unique (org, report_id)) — idempotent with
  conflict on changed inputs; audited
  `profitability_reports.recorded`.

Routes (CLIP-033 pattern): POST /profitability/reports, GET
/profitability/reports?org_id=. No LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.cost_ledger import CostEntryRecord
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base
from clipos_api.revenue_ledger import CURRENCY_PATTERN, RevenueEntryRecord


class Profitability(BaseModel):
    """Pure revenue-minus-cost figures."""

    model_config = ConfigDict(frozen=True)

    revenue_cents: int
    cost_cents: int
    profit_cents: int
    margin_pct: float
    profitable: bool


class ReinvestmentSplit(BaseModel):
    """Pure reinvest/reserve split of one profit figure."""

    model_config = ConfigDict(frozen=True)

    profit_cents: int
    reinvest_pct: float = Field(ge=0.0, le=100.0)
    reinvest_cents: int
    reserve_cents: int
    reason: str = Field(min_length=1, max_length=64)


class ProfitabilityReport(BaseModel):
    """One stored report for one window+currency."""

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    period_start: date
    period_end: date
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    revenue_cents: int
    cost_cents: int
    profit_cents: int
    margin_pct: float
    profitable: bool
    reinvest_pct: float = Field(ge=0.0, le=100.0)
    reinvest_cents: int
    reserve_cents: int
    created_at: datetime | None = None


class RunReportRequest(BaseModel):
    """Request body for one profitability report."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    report_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    period_start: date
    period_end: date
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    reinvest_pct: float = Field(ge=0.0, le=100.0)


def calculate_profitability(revenue_cents: int, cost_cents: int) -> Profitability:
    """Pure profit/margin figures (single-currency totals).

    margin_pct is relative to revenue; zero revenue reports 0% when
    cost is also zero, else -100% (full loss on no revenue).
    """
    profit = revenue_cents - cost_cents
    if revenue_cents == 0:
        margin_pct = 0.0 if cost_cents == 0 else -100.0
    else:
        margin_pct = profit / revenue_cents * 100.0
    return Profitability(
        revenue_cents=revenue_cents,
        cost_cents=cost_cents,
        profit_cents=profit,
        margin_pct=margin_pct,
        profitable=profit >= 0,
    )


def calculate_reinvestment(profit_cents: int, reinvest_pct: float) -> ReinvestmentSplit:
    """Pure integer reinvest/reserve split.

    reinvest = floor(profit * pct / 100) in whole cents; reserve gets
    the remainder. Non-positive profit -> both zeros.
    """
    if not 0.0 <= reinvest_pct <= 100.0:
        raise ValueError("reinvest_pct must be within 0..100")
    if profit_cents <= 0:
        return ReinvestmentSplit(
            profit_cents=profit_cents,
            reinvest_pct=reinvest_pct,
            reinvest_cents=0,
            reserve_cents=0,
            reason="non_positive_profit",
        )
    reinvest = profit_cents * reinvest_pct / 100.0
    reinvest_cents = int(reinvest)  # floor toward zero (profit >= 0)
    reserve_cents = profit_cents - reinvest_cents
    return ReinvestmentSplit(
        profit_cents=profit_cents,
        reinvest_pct=reinvest_pct,
        reinvest_cents=reinvest_cents,
        reserve_cents=reserve_cents,
        reason="split_applied",
    )


class ProfitabilityReportRecord(Base):
    """SQL row for one profitability report."""

    __tablename__ = "profitability_reports"
    __table_args__ = (
        sa.Index(
            "uq_profitability_reports_identity",
            "org_id",
            "report_id",
            unique=True,
        ),
        sa.Index("ix_profitability_reports_org_window", "org_id", "period_start"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    period_start: Mapped[date] = mapped_column(sa.Date, nullable=False)
    period_end: Mapped[date] = mapped_column(sa.Date, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    revenue_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    cost_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    profit_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    margin_pct: Mapped[float] = mapped_column(sa.Float, nullable=False)
    profitable: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    reinvest_pct: Mapped[float] = mapped_column(sa.Float, nullable=False)
    reinvest_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    reserve_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _report_from_row(row: ProfitabilityReportRecord) -> ProfitabilityReport:
    return ProfitabilityReport(
        report_id=row.report_id,
        org_id=row.org_id,
        period_start=row.period_start,
        period_end=row.period_end,
        currency=row.currency,
        revenue_cents=row.revenue_cents,
        cost_cents=row.cost_cents,
        profit_cents=row.profit_cents,
        margin_pct=row.margin_pct,
        profitable=row.profitable,
        reinvest_pct=row.reinvest_pct,
        reinvest_cents=row.reinvest_cents,
        reserve_cents=row.reserve_cents,
        created_at=_aware(row.created_at),
    )


class ProfitabilityStore:
    """Typed persistence for profitability reports."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ProfitabilityStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def run_report(self, request: RunReportRequest) -> ProfitabilityReport:
        """Sum both ledgers for the exact window+currency and compute.

        Idempotent per (org, report_id): repeats replay, changed
        inputs conflict (409); inverted period rejected (422).
        """
        now = datetime.now(UTC)
        if request.period_end < request.period_start:
            raise ValueError("period_end must not precede period_start")
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(ProfitabilityReportRecord).where(
                    ProfitabilityReportRecord.org_id == request.org_id,
                    ProfitabilityReportRecord.report_id == request.report_id,
                )
            ).first()
            if existing is not None:
                stored = _report_from_row(existing)
                if (
                    stored.period_start != request.period_start
                    or stored.period_end != request.period_end
                    or stored.currency != request.currency
                    or stored.reinvest_pct != request.reinvest_pct
                ):
                    raise ValueError(
                        f"profitability report already recorded with different fields: "
                        f"{request.report_id}"
                    )
                return stored
            revenue = int(
                session.scalar(
                    sa.select(
                        sa.func.coalesce(sa.func.sum(RevenueEntryRecord.amount_cents), 0)
                    ).where(
                        RevenueEntryRecord.org_id == request.org_id,
                        RevenueEntryRecord.currency == request.currency,
                        RevenueEntryRecord.period_start == request.period_start,
                        RevenueEntryRecord.period_end == request.period_end,
                    )
                )
                or 0
            )
            cost = int(
                session.scalar(
                    sa.select(sa.func.coalesce(sa.func.sum(CostEntryRecord.amount_cents), 0)).where(
                        CostEntryRecord.org_id == request.org_id,
                        CostEntryRecord.currency == request.currency,
                        CostEntryRecord.period_start == request.period_start,
                        CostEntryRecord.period_end == request.period_end,
                    )
                )
                or 0
            )
        profitability = calculate_profitability(revenue, cost)
        split = calculate_reinvestment(profitability.profit_cents, request.reinvest_pct)
        with self._sessions.begin() as session:
            raced = session.scalars(
                sa.select(ProfitabilityReportRecord).where(
                    ProfitabilityReportRecord.org_id == request.org_id,
                    ProfitabilityReportRecord.report_id == request.report_id,
                )
            ).first()
            if raced is not None:
                return _report_from_row(raced)
            row = ProfitabilityReportRecord(
                report_id=request.report_id,
                org_id=request.org_id,
                period_start=request.period_start,
                period_end=request.period_end,
                currency=request.currency,
                revenue_cents=profitability.revenue_cents,
                cost_cents=profitability.cost_cents,
                profit_cents=profitability.profit_cents,
                margin_pct=profitability.margin_pct,
                profitable=profitability.profitable,
                reinvest_pct=split.reinvest_pct,
                reinvest_cents=split.reinvest_cents,
                reserve_cents=split.reserve_cents,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "profitability_reports.recorded",
                "profitability_reports",
                f"profitability_report:{request.report_id}",
                {
                    "org_id": request.org_id,
                    "currency": request.currency,
                    "profit_cents": profitability.profit_cents,
                    "reinvest_cents": split.reinvest_cents,
                },
            )
            return _report_from_row(row)

    def list_reports(self, org_id: str) -> list[ProfitabilityReport]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ProfitabilityReportRecord)
                .where(ProfitabilityReportRecord.org_id == org_id)
                .order_by(ProfitabilityReportRecord.created_at, ProfitabilityReportRecord.id)
            ).all()
            return [_report_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["profitability"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ProfitabilityStore:
    return ProfitabilityStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "already" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post(
    "/profitability/reports",
    response_model=ProfitabilityReport,
    name="run_profitability_report",
)
def run_profitability_report_route(
    request: RunReportRequest,
    store: Annotated[ProfitabilityStore, Depends(_store)],
) -> ProfitabilityReport:
    """Compute and persist profitability + reinvest split for one window."""
    try:
        return store.run_report(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/profitability/reports",
    response_model=list[ProfitabilityReport],
    name="list_profitability_reports",
)
def list_profitability_reports_route(
    store: Annotated[ProfitabilityStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[ProfitabilityReport]:
    """List persisted profitability reports for one org."""
    return store.list_reports(org_id)
