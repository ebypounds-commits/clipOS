"""Channel incubation/launch experiments (CLIP-079).

PRD journey "discover and produce" + brand-kit rollout — guarded
lifecycle for incubating a new channel before launch:

- **Runs** — `incubation_runs` rows are org-scoped, idempotent per
  (org_id, run_id) with slug ids: a platform slug, an opaque
  channel_ref for the channel under incubation, an optional goal_ref,
  and optional links to a CLIP-078 brand template and channel
  template (both validated in the same org, 404 otherwise).
- **Guarded lifecycle** — pure `run_transition_allowed` table:
  DRAFT -> INCUBATING (start), INCUBATING -> LAUNCHED (launch) or
  SUNSET (abandon), LAUNCHED -> ARCHIVED, SUNSET -> ARCHIVED.
  Launch therefore always travels through INCUBATING — a run can
  never launch straight from DRAFT (409); same-state is a no-op.
  Every real move audits `incubation_runs.status_changed` with
  actor_ref/reason_ref.
- **Audits** — `incubation_runs.registered` on creation.

Migration 0057 (CREATE-only). Routes (CLIP-033 pattern): POST
/incubation-runs, GET /incubation-runs?org_id=&status=, GET
/incubation-runs/{run_id} (404), POST
/incubation-runs/{run_id}/transition. No LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.brand_templates import BrandTemplateRecord, ChannelTemplateRecord
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base


class IncubationStatus(str, Enum):
    """Channel incubation lifecycle."""

    DRAFT = "DRAFT"
    INCUBATING = "INCUBATING"
    LAUNCHED = "LAUNCHED"
    SUNSET = "SUNSET"
    ARCHIVED = "ARCHIVED"


RUN_TRANSITIONS: dict[IncubationStatus, frozenset[IncubationStatus]] = {
    IncubationStatus.DRAFT: frozenset({IncubationStatus.INCUBATING}),
    IncubationStatus.INCUBATING: frozenset({IncubationStatus.LAUNCHED, IncubationStatus.SUNSET}),
    IncubationStatus.LAUNCHED: frozenset({IncubationStatus.ARCHIVED}),
    IncubationStatus.SUNSET: frozenset({IncubationStatus.ARCHIVED}),
    IncubationStatus.ARCHIVED: frozenset(),
}


def run_transition_allowed(current: IncubationStatus, target: IncubationStatus) -> bool:
    """Pure: legal incubation status move (same-state counts as no-op)."""
    if current is target:
        return True
    return target in RUN_TRANSITIONS[current]


class IncubationRun(BaseModel):
    """One channel incubation/launch run."""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    channel_ref: str = Field(min_length=1, max_length=255)
    goal_ref: str | None = Field(default=None, max_length=512)
    brand_template_id: str | None = Field(default=None, max_length=128)
    channel_template_id: str | None = Field(default=None, max_length=128)
    status: IncubationStatus = IncubationStatus.DRAFT
    created_at: datetime | None = None


class RegisterRunRequest(BaseModel):
    """Request body for registering one incubation run."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    platform: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    channel_ref: str = Field(min_length=1, max_length=255)
    goal_ref: str | None = Field(default=None, max_length=512)
    brand_template_id: str | None = Field(default=None, max_length=128)
    channel_template_id: str | None = Field(default=None, max_length=128)


class RunTransitionRequest(BaseModel):
    """Request body for one incubation status transition."""

    model_config = ConfigDict(frozen=True)

    status: IncubationStatus
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)


class IncubationRunRecord(Base):
    """SQL row for one incubation run."""

    __tablename__ = "incubation_runs"
    __table_args__ = (
        sa.Index("uq_incubation_runs_org_run", "org_id", "run_id", unique=True),
        sa.Index("ix_incubation_runs_org_status", "org_id", "status"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    channel_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    goal_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    brand_template_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    channel_template_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _run_from_row(row: IncubationRunRecord) -> IncubationRun:
    return IncubationRun(
        run_id=row.run_id,
        org_id=row.org_id,
        platform=row.platform,
        channel_ref=row.channel_ref,
        goal_ref=row.goal_ref,
        brand_template_id=row.brand_template_id,
        channel_template_id=row.channel_template_id,
        status=IncubationStatus(row.status),
        created_at=_aware(row.created_at),
    )


class IncubationStore:
    """Typed persistence for channel incubation runs."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> IncubationStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_run(self, request: RegisterRunRequest) -> IncubationRun:
        """Idempotent per (org, run_id); template links validated."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            if request.brand_template_id is not None:
                brand = session.scalars(
                    sa.select(BrandTemplateRecord).where(
                        BrandTemplateRecord.org_id == request.org_id,
                        BrandTemplateRecord.template_id == request.brand_template_id,
                    )
                ).first()
                if brand is None:
                    raise ValueError(f"brand template not found: {request.brand_template_id}")
            if request.channel_template_id is not None:
                channel = session.scalars(
                    sa.select(ChannelTemplateRecord).where(
                        ChannelTemplateRecord.org_id == request.org_id,
                        ChannelTemplateRecord.template_id == request.channel_template_id,
                    )
                ).first()
                if channel is None:
                    raise ValueError(f"channel template not found: {request.channel_template_id}")
            existing = session.scalars(
                sa.select(IncubationRunRecord).where(
                    IncubationRunRecord.org_id == request.org_id,
                    IncubationRunRecord.run_id == request.run_id,
                )
            ).first()
            if existing is not None:
                stored = _run_from_row(existing)
                if (
                    stored.platform != request.platform
                    or stored.channel_ref != request.channel_ref
                    or stored.goal_ref != request.goal_ref
                    or stored.brand_template_id != request.brand_template_id
                    or stored.channel_template_id != request.channel_template_id
                ):
                    raise ValueError(
                        f"incubation run already registered with different fields: {request.run_id}"
                    )
                return stored
            row = IncubationRunRecord(
                run_id=request.run_id,
                org_id=request.org_id,
                platform=request.platform,
                channel_ref=request.channel_ref,
                goal_ref=request.goal_ref,
                brand_template_id=request.brand_template_id,
                channel_template_id=request.channel_template_id,
                status=IncubationStatus.DRAFT.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "incubation_runs.registered",
                "incubation_runs",
                f"incubation_run:{request.run_id}",
                {
                    "org_id": request.org_id,
                    "platform": request.platform,
                    "channel_ref": request.channel_ref,
                },
            )
            return _run_from_row(row)

    def get_run(self, run_id: str, org_id: str) -> IncubationRun | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(IncubationRunRecord).where(
                    IncubationRunRecord.run_id == run_id,
                    IncubationRunRecord.org_id == org_id,
                )
            ).first()
            return _run_from_row(row) if row is not None else None

    def list_runs(self, org_id: str, status: IncubationStatus | None = None) -> list[IncubationRun]:
        with self._sessions() as session:
            stmt = sa.select(IncubationRunRecord).where(IncubationRunRecord.org_id == org_id)
            if status is not None:
                stmt = stmt.where(IncubationRunRecord.status == status.value)
            rows = session.scalars(
                stmt.order_by(IncubationRunRecord.created_at, IncubationRunRecord.id)
            ).all()
            return [_run_from_row(r) for r in rows]

    def transition_run(
        self, run_id: str, org_id: str, request: RunTransitionRequest
    ) -> IncubationRun:
        """Apply a validated incubation transition (404/409)."""
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(IncubationRunRecord).where(
                    IncubationRunRecord.run_id == run_id,
                    IncubationRunRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"incubation run not found: {run_id}")
            current = IncubationStatus(row.status)
            target = request.status
            if not run_transition_allowed(current, target):
                raise ValueError(
                    f"illegal incubation transition: {current.value} -> {target.value}"
                )
            if current is not target:
                row.status = target.value
                session.flush()
                audit.record(
                    "incubation_runs.status_changed",
                    "incubation_runs",
                    f"incubation_run:{run_id}",
                    {
                        "org_id": org_id,
                        "from_status": current.value,
                        "to_status": target.value,
                        "actor_ref": request.actor_ref,
                        "reason_ref": request.reason_ref,
                    },
                )
            return _run_from_row(row)


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["incubation"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> IncubationStore:
    return IncubationStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already" in message or "illegal" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post(
    "/incubation-runs",
    response_model=IncubationRun,
    name="register_incubation_run",
)
def register_incubation_run_route(
    request: RegisterRunRequest,
    store: Annotated[IncubationStore, Depends(_store)],
) -> IncubationRun:
    """Register one incubation run (idempotent; template links validated)."""
    try:
        return store.register_run(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/incubation-runs/{run_id}",
    response_model=IncubationRun,
    name="get_incubation_run",
)
def get_incubation_run_route(
    run_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[IncubationStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> IncubationRun:
    """Get one incubation run by id and org."""
    stored = store.get_run(run_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"incubation run not found: {run_id}")
    return stored


@router.get(
    "/incubation-runs",
    response_model=list[IncubationRun],
    name="list_incubation_runs",
)
def list_incubation_runs_route(
    store: Annotated[IncubationStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    status: Annotated[IncubationStatus | None, Query()] = None,
) -> list[IncubationRun]:
    """List incubation runs for one org, optionally filtered by status."""
    return store.list_runs(org_id, status)


@router.post(
    "/incubation-runs/{run_id}/transition",
    response_model=IncubationRun,
    name="transition_incubation_run",
)
def transition_incubation_run_route(
    run_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: RunTransitionRequest,
    store: Annotated[IncubationStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> IncubationRun:
    """Apply an incubation transition (404 unknown, 409 illegal).

    start = DRAFT->INCUBATING, launch = INCUBATING->LAUNCHED,
    sunset = INCUBATING->SUNSET, archive = LAUNCHED/SUNSET->ARCHIVED.
    """
    try:
        return store.transition_run(run_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc
