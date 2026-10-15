"""Incident lifecycle and runbooks (CLIP-087).

PRD §K operations — declared incident response with bound runbooks:

- **Runbooks** — `runbooks` rows are org-scoped, idempotent per
  (org_id, runbook_id) with slug ids: a title, a generic category
  slug, and ordered step rows (`runbook_steps`, append-only, unique
  (org_id, runbook_id, step_no)) so a runbook is a numbered, stable
  procedure rather than free text. Audited `runbooks.registered`.
- **Incidents** — `incidents` rows are org-scoped with client slug ids
  and a severity (SEV1..SEV4). Opening an incident requires the
  subsystem it names and optionally binds a runbook (must exist in the
  same org, 404 otherwise) whose category must match the incident
  category (409 on mismatch). Lifecycle is a pure transition table:
  OPEN -> ACKNOWLEDGED -> MITIGATED -> RESOLVED -> CLOSED, with
  ACKNOWLEDGED/MITIGATED -> CLOSED (dismiss), MITIGATED -> OPEN
  (regression reopen); CLOSED is terminal; same-state no-op;
  anything else 409. Every move audits
  `incidents.status_changed` with actor/reason refs; opening audits
  `incidents.opened`.
- **Timeline** — `incident_timeline` is an append-only, idempotent
  (org, entry_id) log of incident events (opened, status moves,
  operator notes) with a kind slug and bounded note_ref — references
  only, never secrets. Audited `incident_timeline.recorded`.

Migration 0064 (CREATE-only). Routes (CLIP-033 pattern): POST/GET
/runbooks, GET /runbooks/{runbook_id} (with steps), POST/GET
/incidents, GET /incidents/{incident_id},
POST /incidents/{incident_id}/transition, POST/GET
/incidents/{incident_id}/timeline. No LLM, no network.
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
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base


class Severity(str, Enum):
    """Incident severity (PRD §K)."""

    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"


class IncidentStatus(str, Enum):
    """Incident lifecycle."""

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    MITIGATED = "MITIGATED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


INCIDENT_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.OPEN: frozenset({IncidentStatus.ACKNOWLEDGED}),
    IncidentStatus.ACKNOWLEDGED: frozenset({IncidentStatus.MITIGATED, IncidentStatus.CLOSED}),
    IncidentStatus.MITIGATED: frozenset(
        {IncidentStatus.RESOLVED, IncidentStatus.CLOSED, IncidentStatus.OPEN}
    ),
    IncidentStatus.RESOLVED: frozenset({IncidentStatus.CLOSED}),
    IncidentStatus.CLOSED: frozenset(),
}


def incident_transition_allowed(current: IncidentStatus, target: IncidentStatus) -> bool:
    """Pure: legal incident move (same-state counts as no-op)."""
    if current is target:
        return True
    return target in INCIDENT_TRANSITIONS[current]


class RunbookStep(BaseModel):
    """One numbered runbook step."""

    model_config = ConfigDict(frozen=True)

    step_no: int = Field(ge=1, le=200)
    instruction: str = Field(min_length=1, max_length=2000)
    command_ref: str | None = Field(default=None, max_length=512)


class Runbook(BaseModel):
    """One declared incident runbook."""

    model_config = ConfigDict(frozen=True)

    runbook_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    category: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    steps: list[RunbookStep] = Field(default_factory=list)
    created_at: datetime | None = None


class Incident(BaseModel):
    """One incident record."""

    model_config = ConfigDict(frozen=True)

    incident_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    subsystem: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    category: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    severity: Severity
    status: IncidentStatus = IncidentStatus.OPEN
    runbook_id: str | None = Field(default=None, max_length=128)
    created_at: datetime | None = None


class TimelineEntry(BaseModel):
    """One append-only incident timeline entry."""

    model_config = ConfigDict(frozen=True)

    entry_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    incident_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    kind: str = Field(min_length=1, max_length=32, pattern=SLUG_PATTERN)
    note_ref: str | None = Field(default=None, max_length=512)
    actor_ref: str = Field(min_length=1, max_length=255)
    created_at: datetime | None = None


class RegisterRunbookRequest(BaseModel):
    """Request body for registering one runbook."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    runbook_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    category: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    steps: list[RunbookStep] = Field(min_length=1, max_length=200)


class OpenIncidentRequest(BaseModel):
    """Request body for opening one incident."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    incident_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    subsystem: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    category: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    severity: Severity
    runbook_id: str | None = Field(default=None, max_length=128)
    actor_ref: str = Field(min_length=1, max_length=255)


class IncidentTransitionRequest(BaseModel):
    """Request body for one incident transition."""

    model_config = ConfigDict(frozen=True)

    status: IncidentStatus
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)


class RecordTimelineRequest(BaseModel):
    """Request body for one timeline entry."""

    model_config = ConfigDict(frozen=True)

    entry_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    kind: str = Field(min_length=1, max_length=32, pattern=SLUG_PATTERN)
    note_ref: str | None = Field(default=None, max_length=512)
    actor_ref: str = Field(min_length=1, max_length=255)


class RunbookRecord(Base):
    """SQL row for one runbook."""

    __tablename__ = "runbooks"
    __table_args__ = (
        sa.Index("uq_runbooks_org_runbook", "org_id", "runbook_id", unique=True),
        sa.Index("ix_runbooks_org_category", "org_id", "category"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    runbook_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    category: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class RunbookStepRecord(Base):
    """SQL row for one runbook step (append-only)."""

    __tablename__ = "runbook_steps"
    __table_args__ = (
        sa.Index(
            "uq_runbook_steps_identity",
            "org_id",
            "runbook_id",
            "step_no",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    runbook_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    step_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    instruction: Mapped[str] = mapped_column(sa.String(2000), nullable=False)
    command_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class IncidentRecord(Base):
    """SQL row for one incident."""

    __tablename__ = "incidents"
    __table_args__ = (
        sa.Index("uq_incidents_org_incident", "org_id", "incident_id", unique=True),
        sa.Index("ix_incidents_org_status", "org_id", "status"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    subsystem: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    category: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    severity: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    runbook_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class IncidentTimelineRecord(Base):
    """SQL row for one timeline entry (append-only)."""

    __tablename__ = "incident_timeline"
    __table_args__ = (
        sa.Index(
            "uq_incident_timeline_identity",
            "org_id",
            "entry_id",
            unique=True,
        ),
        sa.Index("ix_incident_timeline_org_incident", "org_id", "incident_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    incident_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    note_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    actor_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _runbook_from_row(row: RunbookRecord, steps: list[RunbookStepRecord]) -> Runbook:
    return Runbook(
        runbook_id=row.runbook_id,
        org_id=row.org_id,
        title=row.title,
        category=row.category,
        steps=[
            RunbookStep(
                step_no=s.step_no,
                instruction=s.instruction,
                command_ref=s.command_ref,
            )
            for s in sorted(steps, key=lambda s: s.step_no)
        ],
        created_at=_aware(row.created_at),
    )


def _incident_from_row(row: IncidentRecord) -> Incident:
    return Incident(
        incident_id=row.incident_id,
        org_id=row.org_id,
        title=row.title,
        subsystem=row.subsystem,
        category=row.category,
        severity=Severity(row.severity),
        status=IncidentStatus(row.status),
        runbook_id=row.runbook_id,
        created_at=_aware(row.created_at),
    )


def _timeline_from_row(row: IncidentTimelineRecord) -> TimelineEntry:
    return TimelineEntry(
        entry_id=row.entry_id,
        org_id=row.org_id,
        incident_id=row.incident_id,
        kind=row.kind,
        note_ref=row.note_ref,
        actor_ref=row.actor_ref,
        created_at=_aware(row.created_at),
    )


class IncidentStore:
    """Typed persistence for runbooks, incidents, and timelines."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> IncidentStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # -- runbooks ----------------------------------------------------------

    def register_runbook(self, request: RegisterRunbookRequest) -> Runbook:
        """Idempotent per (org, runbook_id); steps are append-only."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(RunbookRecord).where(
                    RunbookRecord.org_id == request.org_id,
                    RunbookRecord.runbook_id == request.runbook_id,
                )
            ).first()
            if existing is not None:
                stored_steps = session.scalars(
                    sa.select(RunbookStepRecord).where(
                        RunbookStepRecord.org_id == request.org_id,
                        RunbookStepRecord.runbook_id == request.runbook_id,
                    )
                ).all()
                stored = _runbook_from_row(existing, list(stored_steps))
                if (
                    stored.title != request.title
                    or stored.category != request.category
                    or stored.steps != request.steps
                ):
                    raise ValueError(
                        f"runbook already registered with different fields: {request.runbook_id}"
                    )
                return stored
            row = RunbookRecord(
                runbook_id=request.runbook_id,
                org_id=request.org_id,
                title=request.title,
                category=request.category,
                created_at=now,
            )
            session.add(row)
            session.flush()
            for step in request.steps:
                session.add(
                    RunbookStepRecord(
                        org_id=request.org_id,
                        runbook_id=request.runbook_id,
                        step_no=step.step_no,
                        instruction=step.instruction,
                        command_ref=step.command_ref,
                        created_at=now,
                    )
                )
            session.flush()
            audit.record(
                "runbooks.registered",
                "runbooks",
                f"runbook:{request.runbook_id}",
                {
                    "org_id": request.org_id,
                    "category": request.category,
                    "step_count": len(request.steps),
                },
            )
            return _runbook_from_row(row, []).model_copy(update={"steps": list(request.steps)})

    def get_runbook(self, runbook_id: str, org_id: str) -> Runbook | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(RunbookRecord).where(
                    RunbookRecord.runbook_id == runbook_id,
                    RunbookRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                return None
            steps = session.scalars(
                sa.select(RunbookStepRecord).where(
                    RunbookStepRecord.org_id == org_id,
                    RunbookStepRecord.runbook_id == runbook_id,
                )
            ).all()
            return _runbook_from_row(row, list(steps))

    def list_runbooks(self, org_id: str) -> list[Runbook]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(RunbookRecord)
                .where(RunbookRecord.org_id == org_id)
                .order_by(RunbookRecord.created_at, RunbookRecord.id)
            ).all()
            out: list[Runbook] = []
            for row in rows:
                steps = session.scalars(
                    sa.select(RunbookStepRecord).where(
                        RunbookStepRecord.org_id == org_id,
                        RunbookStepRecord.runbook_id == row.runbook_id,
                    )
                ).all()
                out.append(_runbook_from_row(row, list(steps)))
            return out

    # -- incidents ---------------------------------------------------------

    def open_incident(self, request: OpenIncidentRequest) -> Incident:
        """Open one incident; optional runbook must match category."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            if request.runbook_id is not None:
                runbook = session.scalars(
                    sa.select(RunbookRecord).where(
                        RunbookRecord.org_id == request.org_id,
                        RunbookRecord.runbook_id == request.runbook_id,
                    )
                ).first()
                if runbook is None:
                    raise ValueError(f"runbook not found: {request.runbook_id}")
                if runbook.category != request.category:
                    raise ValueError(
                        f"runbook category mismatch: {request.runbook_id} is "
                        f"{runbook.category}, incident is {request.category}"
                    )
            existing = session.scalars(
                sa.select(IncidentRecord).where(
                    IncidentRecord.org_id == request.org_id,
                    IncidentRecord.incident_id == request.incident_id,
                )
            ).first()
            if existing is not None:
                stored = _incident_from_row(existing)
                if (
                    stored.title != request.title
                    or stored.subsystem != request.subsystem
                    or stored.category != request.category
                    or stored.severity != request.severity
                    or stored.runbook_id != request.runbook_id
                ):
                    raise ValueError(
                        f"incident already opened with different fields: {request.incident_id}"
                    )
                return stored
            row = IncidentRecord(
                incident_id=request.incident_id,
                org_id=request.org_id,
                title=request.title,
                subsystem=request.subsystem,
                category=request.category,
                severity=request.severity.value,
                status=IncidentStatus.OPEN.value,
                runbook_id=request.runbook_id,
                created_at=now,
            )
            session.add(row)
            session.flush()
            session.add(
                IncidentTimelineRecord(
                    entry_id=f"open-{request.incident_id}"[:128],
                    org_id=request.org_id,
                    incident_id=request.incident_id,
                    kind="opened",
                    note_ref=None,
                    actor_ref=request.actor_ref,
                    created_at=now,
                )
            )
            session.flush()
            audit.record(
                "incidents.opened",
                "incidents",
                f"incident:{request.incident_id}",
                {
                    "org_id": request.org_id,
                    "subsystem": request.subsystem,
                    "severity": request.severity.value,
                    "runbook_id": request.runbook_id,
                },
            )
            return _incident_from_row(row)

    def get_incident(self, incident_id: str, org_id: str) -> Incident | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(IncidentRecord).where(
                    IncidentRecord.incident_id == incident_id,
                    IncidentRecord.org_id == org_id,
                )
            ).first()
            return _incident_from_row(row) if row is not None else None

    def list_incidents(self, org_id: str, status: IncidentStatus | None = None) -> list[Incident]:
        with self._sessions() as session:
            stmt = sa.select(IncidentRecord).where(IncidentRecord.org_id == org_id)
            if status is not None:
                stmt = stmt.where(IncidentRecord.status == status.value)
            rows = session.scalars(
                stmt.order_by(IncidentRecord.created_at, IncidentRecord.id)
            ).all()
            return [_incident_from_row(r) for r in rows]

    def transition_incident(
        self, incident_id: str, org_id: str, request: IncidentTransitionRequest
    ) -> Incident:
        """Apply a validated incident move; records a timeline entry."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(IncidentRecord).where(
                    IncidentRecord.incident_id == incident_id,
                    IncidentRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"incident not found: {incident_id}")
            current = IncidentStatus(row.status)
            target = request.status
            if not incident_transition_allowed(current, target):
                raise ValueError(f"illegal incident transition: {current.value} -> {target.value}")
            if current is not target:
                row.status = target.value
                session.flush()
                session.add(
                    IncidentTimelineRecord(
                        entry_id=f"{incident_id}-{target.value.lower()}"[:128],
                        org_id=org_id,
                        incident_id=incident_id,
                        kind="status",
                        note_ref=request.reason_ref,
                        actor_ref=request.actor_ref,
                        created_at=now,
                    )
                )
                session.flush()
                audit.record(
                    "incidents.status_changed",
                    "incidents",
                    f"incident:{incident_id}",
                    {
                        "org_id": org_id,
                        "from_status": current.value,
                        "to_status": target.value,
                        "actor_ref": request.actor_ref,
                        "reason_ref": request.reason_ref,
                    },
                )
            return _incident_from_row(row)

    # -- timeline ----------------------------------------------------------

    def record_timeline(
        self, incident_id: str, org_id: str, request: RecordTimelineRequest
    ) -> TimelineEntry:
        """Append one timeline entry; incident must exist (404)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            incident = session.scalars(
                sa.select(IncidentRecord).where(
                    IncidentRecord.incident_id == incident_id,
                    IncidentRecord.org_id == org_id,
                )
            ).first()
            if incident is None:
                raise ValueError(f"incident not found: {incident_id}")
            existing = session.scalars(
                sa.select(IncidentTimelineRecord).where(
                    IncidentTimelineRecord.org_id == org_id,
                    IncidentTimelineRecord.entry_id == request.entry_id,
                )
            ).first()
            if existing is not None:
                stored = _timeline_from_row(existing)
                if (
                    stored.incident_id != incident_id
                    or stored.kind != request.kind
                    or stored.note_ref != request.note_ref
                    or stored.actor_ref != request.actor_ref
                ):
                    raise ValueError(
                        f"timeline entry already recorded with different fields: {request.entry_id}"
                    )
                return stored
            row = IncidentTimelineRecord(
                entry_id=request.entry_id,
                org_id=org_id,
                incident_id=incident_id,
                kind=request.kind,
                note_ref=request.note_ref,
                actor_ref=request.actor_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "incident_timeline.recorded",
                "incident_timeline",
                f"incident_timeline:{request.entry_id}",
                {"org_id": org_id, "incident_id": incident_id, "kind": request.kind},
            )
            return _timeline_from_row(row)

    def list_timeline(self, incident_id: str, org_id: str) -> list[TimelineEntry]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(IncidentTimelineRecord)
                .where(
                    IncidentTimelineRecord.org_id == org_id,
                    IncidentTimelineRecord.incident_id == incident_id,
                )
                .order_by(IncidentTimelineRecord.created_at, IncidentTimelineRecord.id)
            ).all()
            return [_timeline_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["incidents"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> IncidentStore:
    return IncidentStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already" in message or "illegal" in message or "mismatch" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post("/runbooks", response_model=Runbook, name="register_runbook")
def register_runbook_route(
    request: RegisterRunbookRequest,
    store: Annotated[IncidentStore, Depends(_store)],
) -> Runbook:
    """Register one runbook with ordered steps (idempotent)."""
    try:
        return store.register_runbook(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/runbooks", response_model=list[Runbook], name="list_runbooks")
def list_runbooks_route(
    store: Annotated[IncidentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[Runbook]:
    """List runbooks for one org."""
    return store.list_runbooks(org_id)


@router.get("/runbooks/{runbook_id}", response_model=Runbook, name="get_runbook")
def get_runbook_route(
    runbook_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[IncidentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Runbook:
    """Get one runbook (with steps) by id and org."""
    stored = store.get_runbook(runbook_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"runbook not found: {runbook_id}")
    return stored


@router.post("/incidents", response_model=Incident, name="open_incident")
def open_incident_route(
    request: OpenIncidentRequest,
    store: Annotated[IncidentStore, Depends(_store)],
) -> Incident:
    """Open one incident (optional runbook must match category, 409)."""
    try:
        return store.open_incident(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/incidents", response_model=list[Incident], name="list_incidents")
def list_incidents_route(
    store: Annotated[IncidentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    status: Annotated[IncidentStatus | None, Query()] = None,
) -> list[Incident]:
    """List incidents for one org, optionally filtered by status."""
    return store.list_incidents(org_id, status)


@router.get("/incidents/{incident_id}", response_model=Incident, name="get_incident")
def get_incident_route(
    incident_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[IncidentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Incident:
    """Get one incident by id and org."""
    stored = store.get_incident(incident_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"incident not found: {incident_id}")
    return stored


@router.post(
    "/incidents/{incident_id}/transition",
    response_model=Incident,
    name="transition_incident",
)
def transition_incident_route(
    incident_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: IncidentTransitionRequest,
    store: Annotated[IncidentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Incident:
    """Apply an incident transition (404 unknown, 409 illegal)."""
    try:
        return store.transition_incident(incident_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/incidents/{incident_id}/timeline",
    response_model=TimelineEntry,
    name="record_incident_timeline",
)
def record_incident_timeline_route(
    incident_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: RecordTimelineRequest,
    store: Annotated[IncidentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> TimelineEntry:
    """Append one incident timeline entry (refs only)."""
    try:
        return store.record_timeline(incident_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/incidents/{incident_id}/timeline",
    response_model=list[TimelineEntry],
    name="list_incident_timeline",
)
def list_incident_timeline_route(
    incident_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[IncidentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[TimelineEntry]:
    """List timeline entries for one incident."""
    return store.list_timeline(incident_id, org_id)
