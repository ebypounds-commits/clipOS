"""LLM Gateway cost ledger: operator-visible dispatch persistence and spend sums.

Every gateway dispatch attempt is recorded once per ``(execution_id, provider,
model_ref)``, so a fallback chain leaves one auditable row per attempted route.
Recording is idempotent: re-recording the same attempted route returns the
stored row and never duplicates cost.  ``sum_costs`` gives operators a
windowed spend total so spend ceilings can be evaluated from persisted rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api.agent_registry import database_engine
from clipos_api.llm_gateway import Usage
from clipos_api.repository import Base, _aware

_DEFAULT_WINDOW_HOURS = 24
_MAX_WINDOW_HOURS = 24 * 30
_MAX_LIMIT = 200


class DispatchOutcome(str, Enum):
    """Outcome of one attempted route in one dispatch execution."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEGRADED_FALLBACK = "degraded_fallback"


class DispatchRecord(BaseModel):
    """One operator-visible, cost-bearing dispatch attempt."""

    model_config = ConfigDict(frozen=True)

    execution_id: str = Field(min_length=1, max_length=128)
    alias: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=255)
    model_ref: str = Field(min_length=1, max_length=255)
    outcome: DispatchOutcome
    latency_ms: int = Field(default=0, ge=0)
    usage: Usage = Field(default_factory=Usage)
    cost_estimate: float = Field(default=0.0, ge=0.0)
    at: datetime


class DispatchRecordModel(Base):
    """SQL row for one dispatch attempt; identity is execution plus route."""

    __tablename__ = "llm_dispatch_records"
    __table_args__ = (
        sa.Index(
            "uq_llm_dispatch_records_identity",
            "execution_id",
            "provider",
            "model_ref",
            unique=True,
        ),
        sa.Index("ix_llm_dispatch_records_alias_at", "alias", "at"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    alias: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    model_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    latency_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    usage: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    cost_estimate: Mapped[float] = mapped_column(sa.Float, nullable=False)
    at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _record_from_row(row: DispatchRecordModel) -> DispatchRecord:
    return DispatchRecord(
        execution_id=row.execution_id,
        alias=row.alias,
        provider=row.provider,
        model_ref=row.model_ref,
        outcome=DispatchOutcome(row.outcome),
        latency_ms=row.latency_ms,
        usage=Usage(**row.usage),
        cost_estimate=row.cost_estimate,
        at=_aware(row.at),
    )


class CostLedger:
    """Session-bound append-only persistence for gateway dispatch attempts."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> CostLedger:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    @staticmethod
    def _row(session: Session, record: DispatchRecord) -> DispatchRecordModel | None:
        return session.scalars(
            sa.select(DispatchRecordModel).where(
                DispatchRecordModel.execution_id == record.execution_id,
                DispatchRecordModel.provider == record.provider,
                DispatchRecordModel.model_ref == record.model_ref,
            )
        ).first()

    def record_in_transaction(self, session: Session, record: DispatchRecord) -> DispatchRecord:
        """Idempotently append one dispatch attempt; duplicates return the stored row."""
        existing = self._row(session, record)
        if existing is not None:
            return _record_from_row(existing)
        row = DispatchRecordModel(
            execution_id=record.execution_id,
            alias=record.alias,
            provider=record.provider,
            model_ref=record.model_ref,
            outcome=record.outcome.value,
            latency_ms=record.latency_ms,
            usage=record.usage.model_dump(),
            cost_estimate=record.cost_estimate,
            at=record.at,
        )
        session.add(row)
        session.flush()
        return _record_from_row(row)

    def record(self, record: DispatchRecord) -> DispatchRecord:
        """Append one dispatch attempt; re-recording is a no-op."""
        with self._sessions.begin() as session:
            return self.record_in_transaction(session, record)

    def sum_costs(self, alias: str, window: timedelta) -> float:
        """Sum estimated spend for one alias over the trailing window."""
        if window <= timedelta(0):
            raise ValueError("window must be positive")
        since = datetime.now(UTC) - window
        with self._sessions() as session:
            total = session.scalar(
                sa.select(
                    sa.func.coalesce(sa.func.sum(DispatchRecordModel.cost_estimate), 0.0)
                ).where(
                    DispatchRecordModel.alias == alias,
                    DispatchRecordModel.at >= since,
                )
            )
            return float(total or 0.0)

    def recent_dispatches(self, alias: str, limit: int = 20) -> list[DispatchRecord]:
        """Return the most recent dispatch rows for one alias, newest first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(DispatchRecordModel)
                .where(DispatchRecordModel.alias == alias)
                .order_by(DispatchRecordModel.at.desc(), DispatchRecordModel.id.desc())
                .limit(limit)
            ).all()
            return [_record_from_row(row) for row in rows]


class WindowSummary(BaseModel):
    """Operator-visible windowed cost summary with recent dispatch rows."""

    model_config = ConfigDict(frozen=True)

    alias: str
    window_hours: int
    total_cost_estimate: float
    recent: list[DispatchRecord]


def _ledger(engine: Annotated[Engine, Depends(database_engine)]) -> CostLedger:
    return CostLedger.from_engine(engine)


def get_alias_costs(
    ledger: Annotated[CostLedger, Depends(_ledger)],
    alias: str,
    window_hours: int = _DEFAULT_WINDOW_HOURS,
) -> WindowSummary:
    """Return windowed spend and recent dispatch rows for one alias."""
    if window_hours < 1 or window_hours > _MAX_WINDOW_HOURS:
        raise HTTPException(
            status_code=400,
            detail=f"window_hours must be between 1 and {_MAX_WINDOW_HOURS}",
        )
    window = timedelta(hours=window_hours)
    return WindowSummary(
        alias=alias,
        window_hours=window_hours,
        total_cost_estimate=ledger.sum_costs(alias, window),
        recent=ledger.recent_dispatches(alias, limit=_MAX_LIMIT),
    )


def get_alias_dispatches(
    ledger: Annotated[CostLedger, Depends(_ledger)],
    alias: str,
    limit: int = 20,
) -> list[DispatchRecord]:
    """Return recent operator-visible dispatch rows for one alias."""
    if limit < 1 or limit > _MAX_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit must be between 1 and {_MAX_LIMIT}")
    return ledger.recent_dispatches(alias, limit=limit)


router = APIRouter(prefix="/models", tags=["llm-cost"])
router.add_api_route(
    "/{alias}/costs", get_alias_costs, methods=["GET"], name="get_model_alias_costs"
)
router.add_api_route(
    "/{alias}/dispatches", get_alias_dispatches, methods=["GET"], name="get_model_alias_dispatches"
)
