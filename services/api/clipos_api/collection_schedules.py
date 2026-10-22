"""Scheduled analytics collection (CLIP-065).

Operator-defined collection schedules for published content (PRD §H
"schedules" + journey 3 "schedules analytics collection"):

- **Schedule contract** — one `collection_schedules` row per
  (org_id, content_ref): platform/account/external refs, interval in
  minutes (5..10080), enabled flag, last/next run timestamps.
  Registration is idempotent on the natural key; changed fields
  conflict (409).
- **Due-run executor** — `run_due` takes schedules whose next_run_at
  has arrived (injected clock — pure Python comparison, timezone-safe),
  collects metrics through the CLIP-062 AnalyticsAdapter flow, then
  ALWAYS advances last_run_at/next_run_at — including on adapter
  failure (fail-soft: a failing schedule cannot hot-loop; the outcome
  carries the error). Caller-invoked via POST
  /collection-schedules/run-due (the scheduler tick is an operator or
  workflow concern; no background thread).

Audits: `collection_schedules.registered`, `collection_schedules.executed`.
Routes: register, list, run-due. Migration: 0043 (CREATE-only).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.analytics import (
    AnalyticsAdapter,
    AnalyticsStore,
    CollectRequest,
    MockAnalyticsAdapter,
    collect_analytics,
)
from clipos_api.repository import Base

MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 10080


class CollectionSchedule(BaseModel):
    """One scheduled analytics collection for one content_ref."""

    model_config = ConfigDict(frozen=True)

    schedule_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    account_ref: str = Field(min_length=1, max_length=255)
    content_ref: str = Field(min_length=1, max_length=512)
    external_id: str = Field(min_length=1, max_length=255)
    interval_minutes: int = Field(ge=MIN_INTERVAL_MINUTES, le=MAX_INTERVAL_MINUTES)
    enabled: bool = True
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    created_at: datetime | None = None


class RegisterScheduleRequest(BaseModel):
    """Request body for registering a collection schedule."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    account_ref: str = Field(min_length=1, max_length=255)
    content_ref: str = Field(min_length=1, max_length=512)
    external_id: str = Field(min_length=1, max_length=255)
    interval_minutes: int = Field(ge=MIN_INTERVAL_MINUTES, le=MAX_INTERVAL_MINUTES)
    enabled: bool = True


class RunDueRequest(BaseModel):
    """Request body for executing due schedules."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)


class RunOutcome(BaseModel):
    """Outcome of one due-schedule execution."""

    model_config = ConfigDict(frozen=True)

    schedule_id: str
    content_ref: str
    status: str = Field(min_length=1, max_length=16)
    metric_count: int = Field(ge=0)
    error: str | None = Field(default=None, max_length=512)


class RunDueResult(BaseModel):
    """Batch result for one run-due call."""

    model_config = ConfigDict(frozen=True)

    ran: int = Field(ge=0)
    outcomes: list[RunOutcome] = Field(default_factory=list)


class CollectionScheduleRecord(Base):
    """SQL row for one collection schedule."""

    __tablename__ = "collection_schedules"
    __table_args__ = (
        sa.Index(
            "uq_collection_schedules_org_content",
            "org_id",
            "content_ref",
            unique=True,
        ),
        sa.Index("ix_collection_schedules_org_enabled", "org_id", "enabled"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    schedule_id: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    account_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    content_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    external_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    interval_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _schedule_from_row(row: CollectionScheduleRecord) -> CollectionSchedule:
    return CollectionSchedule(
        schedule_id=row.schedule_id,
        org_id=row.org_id,
        platform=row.platform,
        account_ref=row.account_ref,
        content_ref=row.content_ref,
        external_id=row.external_id,
        interval_minutes=row.interval_minutes,
        enabled=row.enabled,
        last_run_at=_aware(row.last_run_at),
        next_run_at=_aware(row.next_run_at),
        created_at=_aware(row.created_at),
    )


class CollectionScheduleStore:
    """Typed persistence for collection schedules."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> CollectionScheduleStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_schedule(self, request: RegisterScheduleRequest) -> CollectionSchedule:
        """Idempotent per (org, content_ref); changed fields conflict."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(CollectionScheduleRecord).where(
                    CollectionScheduleRecord.org_id == request.org_id,
                    CollectionScheduleRecord.content_ref == request.content_ref,
                )
            ).first()
            if existing is not None:
                stored = _schedule_from_row(existing)
                if (
                    stored.platform != request.platform
                    or stored.account_ref != request.account_ref
                    or stored.external_id != request.external_id
                    or stored.interval_minutes != request.interval_minutes
                    or stored.enabled != request.enabled
                ):
                    raise ValueError(
                        f"collection schedule already registered with different fields: "
                        f"{request.content_ref}"
                    )
                return stored
            schedule_id = str(uuid4())[:12]
            row = CollectionScheduleRecord(
                schedule_id=schedule_id,
                org_id=request.org_id,
                platform=request.platform,
                account_ref=request.account_ref,
                content_ref=request.content_ref,
                external_id=request.external_id,
                interval_minutes=request.interval_minutes,
                enabled=request.enabled,
                last_run_at=None,
                next_run_at=now + timedelta(minutes=request.interval_minutes),
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "collection_schedules.registered",
                "collection_schedules",
                f"collection_schedule:{schedule_id}",
                {
                    "org_id": request.org_id,
                    "platform": request.platform,
                    "content_ref": request.content_ref,
                    "interval_minutes": request.interval_minutes,
                },
            )
            return _schedule_from_row(row)

    def list_schedules(self, org_id: str, enabled: bool | None = None) -> list[CollectionSchedule]:
        with self._sessions() as session:
            stmt = sa.select(CollectionScheduleRecord).where(
                CollectionScheduleRecord.org_id == org_id
            )
            if enabled is not None:
                stmt = stmt.where(CollectionScheduleRecord.enabled == enabled)
            rows = session.scalars(
                stmt.order_by(CollectionScheduleRecord.created_at, CollectionScheduleRecord.id)
            ).all()
            return [_schedule_from_row(r) for r in rows]

    def run_due(
        self,
        org_id: str,
        adapter: AnalyticsAdapter,
        analytics_store: AnalyticsStore,
        now: datetime | None = None,
    ) -> RunDueResult:
        """Execute enabled schedules whose next_run_at has arrived.

        The clock is injected (default now). Each due schedule collects
        metrics then ALWAYS advances last_run_at/next_run_at — adapter
        failures become FAILED outcomes, never hot-loops.
        """
        current = now if now is not None else datetime.now(UTC)
        current = _aware(current) or current
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(CollectionScheduleRecord).where(
                    CollectionScheduleRecord.org_id == org_id,
                    CollectionScheduleRecord.enabled.is_(True),
                )
            ).all()
        due = [
            row
            for row in rows
            if row.next_run_at is not None and (_aware(row.next_run_at) or current) <= current
        ]
        outcomes: list[RunOutcome] = []
        for row in due:
            error: str | None = None
            status = "collected"
            metric_count = 0
            try:
                snapshot = collect_analytics(
                    adapter,
                    analytics_store,
                    CollectRequest(
                        org_id=row.org_id,
                        platform=row.platform,
                        account_ref=row.account_ref,
                        content_ref=row.content_ref,
                        external_id=row.external_id,
                    ),
                )
                metric_count = len(snapshot.metrics)
            except Exception as exc:  # noqa: BLE001 - fail-soft per schedule
                status = "failed"
                error = str(exc)[:512]
            with self._sessions.begin() as session:
                live = session.scalars(
                    sa.select(CollectionScheduleRecord).where(CollectionScheduleRecord.id == row.id)
                ).first()
                if live is not None:
                    live.last_run_at = current
                    live.next_run_at = current + timedelta(minutes=live.interval_minutes)
            audit.record(
                "collection_schedules.executed",
                "collection_schedules",
                f"collection_schedule:{row.schedule_id}",
                {
                    "org_id": row.org_id,
                    "content_ref": row.content_ref,
                    "status": status,
                    "metric_count": metric_count,
                },
            )
            outcomes.append(
                RunOutcome(
                    schedule_id=row.schedule_id,
                    content_ref=row.content_ref,
                    status=status,
                    metric_count=metric_count,
                    error=error,
                )
            )
        return RunDueResult(ran=len(outcomes), outcomes=outcomes)


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["collection-schedules"])


def _store(
    engine: Annotated[Engine, Depends(_database_engine)],
) -> CollectionScheduleStore:
    return CollectionScheduleStore.from_engine(engine)


def _analytics_store(
    engine: Annotated[Engine, Depends(_database_engine)],
) -> AnalyticsStore:
    return AnalyticsStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _adapter() -> AnalyticsAdapter:
    return MockAnalyticsAdapter()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already registered" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post(
    "/collection-schedules",
    response_model=CollectionSchedule,
    name="register_collection_schedule",
)
def register_collection_schedule_route(
    request: RegisterScheduleRequest,
    store: Annotated[CollectionScheduleStore, Depends(_store)],
) -> CollectionSchedule:
    """Register one collection schedule (idempotent per org+content)."""
    try:
        return store.register_schedule(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/collection-schedules",
    response_model=list[CollectionSchedule],
    name="list_collection_schedules",
)
def list_collection_schedules_route(
    store: Annotated[CollectionScheduleStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    enabled: Annotated[bool | None, Query()] = None,
) -> list[CollectionSchedule]:
    """List collection schedules for one org."""
    return store.list_schedules(org_id, enabled)


@router.post(
    "/collection-schedules/run-due",
    response_model=RunDueResult,
    name="run_due_collection_schedules",
)
def run_due_collection_schedules_route(
    request: RunDueRequest,
    store: Annotated[CollectionScheduleStore, Depends(_store)],
    analytics_store: Annotated[AnalyticsStore, Depends(_analytics_store)],
    adapter: Annotated[AnalyticsAdapter, Depends(_adapter)],
) -> RunDueResult:
    """Execute enabled schedules whose next_run_at has arrived."""
    return store.run_due(request.org_id, adapter, analytics_store)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _run_all_orgs(
    engine: Engine,
    adapter: AnalyticsAdapter,
    analytics_store: AnalyticsStore,
) -> RunDueResult:
    """Run due schedules for all organizations."""
    store = CollectionScheduleStore.from_engine(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        org_rows = (
            session.execute(sa.select(CollectionScheduleRecord.org_id).distinct()).scalars().all()
        )
    combined = RunDueResult(ran=0, outcomes=[])
    for org_id in org_rows:
        result = store.run_due(org_id, adapter, analytics_store)
        combined.ran += result.ran
        combined.outcomes.extend(result.outcomes)
    return combined


def main() -> None:
    """CLI entry point: run due schedules for one org or all orgs."""
    import argparse

    parser = argparse.ArgumentParser(description="Run due collection schedules")
    parser.add_argument(
        "--org-id",
        default="all",
        help="Organization ID to run schedules for, or 'all' for all orgs",
    )
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL not set")
    engine = create_engine(url)
    adapter = MockAnalyticsAdapter()
    analytics_store = AnalyticsStore.from_engine(engine)

    if args.org_id == "all":
        result = _run_all_orgs(engine, adapter, analytics_store)
    else:
        store = CollectionScheduleStore.from_engine(engine)
        result = store.run_due(args.org_id, adapter, analytics_store)

    for outcome in result.outcomes:
        status = outcome.status.upper()
        print(
            f"[{status}] {outcome.schedule_id} {outcome.content_ref} metrics={outcome.metric_count}"
        )
        if outcome.error:
            print(f"  ERROR: {outcome.error}")
    print(f"Ran {result.ran} schedule(s)")


if __name__ == "__main__":
    main()
