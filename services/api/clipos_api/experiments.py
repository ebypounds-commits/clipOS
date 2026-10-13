"""Experiment and hypothesis data model (CLIP-066, M16 opener).

PRD §H experiments/cohorts foundation — typed experiment records with
audited status lifecycles and linked hypotheses:

- **Experiments** — `experiments` table, org-scoped, idempotent per
  (org_id, experiment_id) with client natural ids (slug-validated:
  [A-Za-z0-9._-]). `target_metric` reuses the CLIP-062 MetricKind (the
  metric the experiment moves). Status lifecycle is a pure transition
  table: DRAFT -> ACTIVE -> COMPLETED -> ARCHIVED, plus DRAFT ->
  ARCHIVED; everything else is rejected (409), same-state is a no-op.
  Audits: `experiments.registered`, `experiments.status_changed`.
- **Hypotheses** — `hypotheses` table linked to one experiment (same
  org enforced, 404 otherwise), idempotent per (org_id,
  hypothesis_id), direction INCREASE/DECREASE/NO_CHANGE against the
  experiment's target metric. Status: PROPOSED -> SUPPORTED or
  REJECTED (one resolution, then frozen; same-state no-op). Audits:
  `hypotheses.registered`, `hypotheses.status_changed`.

Routes (CLIP-033 pattern): POST/GET /experiments, POST
/experiments/{id}/transition, POST/GET /hypotheses, POST
/hypotheses/{id}/transition. No LLM, no network, no provider
identifier; migration 0044 (CREATE-only).
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
from clipos_api.analytics import MetricKind
from clipos_api.repository import Base

SLUG_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"


class ExperimentStatus(str, Enum):
    """Experiment lifecycle (PRD §H)."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    ARCHIVED = "ARCHIVED"


class HypothesisStatus(str, Enum):
    """Hypothesis resolution lifecycle."""

    PROPOSED = "PROPOSED"
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"


class HypothesisDirection(str, Enum):
    """Predicted movement of the experiment's target metric."""

    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    NO_CHANGE = "NO_CHANGE"


EXPERIMENT_TRANSITIONS: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    ExperimentStatus.DRAFT: frozenset({ExperimentStatus.ACTIVE, ExperimentStatus.ARCHIVED}),
    ExperimentStatus.ACTIVE: frozenset({ExperimentStatus.COMPLETED}),
    ExperimentStatus.COMPLETED: frozenset({ExperimentStatus.ARCHIVED}),
    ExperimentStatus.ARCHIVED: frozenset(),
}

HYPOTHESIS_TRANSITIONS: dict[HypothesisStatus, frozenset[HypothesisStatus]] = {
    HypothesisStatus.PROPOSED: frozenset({HypothesisStatus.SUPPORTED, HypothesisStatus.REJECTED}),
    HypothesisStatus.SUPPORTED: frozenset(),
    HypothesisStatus.REJECTED: frozenset(),
}


def experiment_transition_allowed(current: ExperimentStatus, target: ExperimentStatus) -> bool:
    """Pure: legal experiment status move (same-state counts as allowed no-op)."""
    if current is target:
        return True
    return target in EXPERIMENT_TRANSITIONS[current]


def hypothesis_transition_allowed(current: HypothesisStatus, target: HypothesisStatus) -> bool:
    """Pure: legal hypothesis status move (same-state counts as allowed no-op)."""
    if current is target:
        return True
    return target in HYPOTHESIS_TRANSITIONS[current]


class Experiment(BaseModel):
    """One experiment record."""

    model_config = ConfigDict(frozen=True)

    experiment_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    target_metric: MetricKind
    hypothesis_summary: str = Field(min_length=1, max_length=1000)
    status: ExperimentStatus = ExperimentStatus.DRAFT
    created_at: datetime | None = None


class RegisterExperimentRequest(BaseModel):
    """Request body for registering an experiment."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    experiment_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    target_metric: MetricKind
    hypothesis_summary: str = Field(min_length=1, max_length=1000)


class Hypothesis(BaseModel):
    """One hypothesis linked to one experiment."""

    model_config = ConfigDict(frozen=True)

    hypothesis_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    experiment_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    statement: str = Field(min_length=1, max_length=2000)
    direction: HypothesisDirection
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    created_at: datetime | None = None


class RegisterHypothesisRequest(BaseModel):
    """Request body for registering a hypothesis."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    hypothesis_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    experiment_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    statement: str = Field(min_length=1, max_length=2000)
    direction: HypothesisDirection


class TransitionRequest(BaseModel):
    """Request body for a status transition (experiments and hypotheses)."""

    model_config = ConfigDict(frozen=True)

    status: str = Field(min_length=1, max_length=32)
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)


class ExperimentRecord(Base):
    """SQL row for one experiment."""

    __tablename__ = "experiments"
    __table_args__ = (
        sa.Index("uq_experiments_org_experiment", "org_id", "experiment_id", unique=True),
        sa.Index("ix_experiments_org_status", "org_id", "status"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    target_metric: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    hypothesis_summary: Mapped[str] = mapped_column(sa.String(1000), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class HypothesisRecord(Base):
    """SQL row for one hypothesis."""

    __tablename__ = "hypotheses"
    __table_args__ = (
        sa.Index("uq_hypotheses_org_hypothesis", "org_id", "hypothesis_id", unique=True),
        sa.Index("ix_hypotheses_org_experiment", "org_id", "experiment_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    hypothesis_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    statement: Mapped[str] = mapped_column(sa.String(2000), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _experiment_from_row(row: ExperimentRecord) -> Experiment:
    return Experiment(
        experiment_id=row.experiment_id,
        org_id=row.org_id,
        title=row.title,
        target_metric=MetricKind(row.target_metric),
        hypothesis_summary=row.hypothesis_summary,
        status=ExperimentStatus(row.status),
        created_at=_aware(row.created_at),
    )


def _hypothesis_from_row(row: HypothesisRecord) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=row.hypothesis_id,
        org_id=row.org_id,
        experiment_id=row.experiment_id,
        statement=row.statement,
        direction=HypothesisDirection(row.direction),
        status=HypothesisStatus(row.status),
        created_at=_aware(row.created_at),
    )


class ExperimentStore:
    """Typed persistence for experiments and hypotheses."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ExperimentStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # -- experiments -------------------------------------------------------

    def register_experiment(self, request: RegisterExperimentRequest) -> Experiment:
        """Idempotent per (org, experiment_id); changed fields conflict."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(ExperimentRecord).where(
                    ExperimentRecord.org_id == request.org_id,
                    ExperimentRecord.experiment_id == request.experiment_id,
                )
            ).first()
            if existing is not None:
                stored = _experiment_from_row(existing)
                if (
                    stored.title != request.title
                    or stored.target_metric != request.target_metric
                    or stored.hypothesis_summary != request.hypothesis_summary
                ):
                    raise ValueError(
                        f"experiment already registered with different fields: "
                        f"{request.experiment_id}"
                    )
                return stored
            row = ExperimentRecord(
                experiment_id=request.experiment_id,
                org_id=request.org_id,
                title=request.title,
                target_metric=request.target_metric.value,
                hypothesis_summary=request.hypothesis_summary,
                status=ExperimentStatus.DRAFT.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "experiments.registered",
                "experiments",
                f"experiment:{request.experiment_id}",
                {
                    "org_id": request.org_id,
                    "target_metric": request.target_metric.value,
                },
            )
            return _experiment_from_row(row)

    def get_experiment(self, experiment_id: str, org_id: str) -> Experiment | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ExperimentRecord).where(
                    ExperimentRecord.experiment_id == experiment_id,
                    ExperimentRecord.org_id == org_id,
                )
            ).first()
            return _experiment_from_row(row) if row is not None else None

    def list_experiments(
        self, org_id: str, status: ExperimentStatus | None = None
    ) -> list[Experiment]:
        with self._sessions() as session:
            stmt = sa.select(ExperimentRecord).where(ExperimentRecord.org_id == org_id)
            if status is not None:
                stmt = stmt.where(ExperimentRecord.status == status.value)
            rows = session.scalars(
                stmt.order_by(ExperimentRecord.created_at, ExperimentRecord.id)
            ).all()
            return [_experiment_from_row(r) for r in rows]

    def transition_experiment(
        self, request: TransitionRequest, org_id: str, experiment_id: str
    ) -> Experiment:
        """Apply a validated status transition (409 on illegal moves)."""
        target = ExperimentStatus(request.status)
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(ExperimentRecord).where(
                    ExperimentRecord.experiment_id == experiment_id,
                    ExperimentRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"experiment not found: {experiment_id}")
            current = ExperimentStatus(row.status)
            if not experiment_transition_allowed(current, target):
                raise ValueError(
                    f"illegal experiment transition: {current.value} -> {target.value}"
                )
            if current is not target:
                row.status = target.value
                session.flush()
                audit.record(
                    "experiments.status_changed",
                    "experiments",
                    f"experiment:{experiment_id}",
                    {
                        "org_id": org_id,
                        "from_status": current.value,
                        "to_status": target.value,
                        "actor_ref": request.actor_ref,
                        "reason_ref": request.reason_ref,
                    },
                )
            return _experiment_from_row(row)

    # -- hypotheses --------------------------------------------------------

    def register_hypothesis(self, request: RegisterHypothesisRequest) -> Hypothesis:
        """Idempotent per (org, hypothesis_id); requires linked experiment."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            experiment = session.scalars(
                sa.select(ExperimentRecord).where(
                    ExperimentRecord.org_id == request.org_id,
                    ExperimentRecord.experiment_id == request.experiment_id,
                )
            ).first()
            if experiment is None:
                raise ValueError(f"experiment not found: {request.experiment_id}")
            existing = session.scalars(
                sa.select(HypothesisRecord).where(
                    HypothesisRecord.org_id == request.org_id,
                    HypothesisRecord.hypothesis_id == request.hypothesis_id,
                )
            ).first()
            if existing is not None:
                stored = _hypothesis_from_row(existing)
                if (
                    stored.experiment_id != request.experiment_id
                    or stored.statement != request.statement
                    or stored.direction != request.direction
                ):
                    raise ValueError(
                        f"hypothesis already registered with different fields: "
                        f"{request.hypothesis_id}"
                    )
                return stored
            row = HypothesisRecord(
                hypothesis_id=request.hypothesis_id,
                org_id=request.org_id,
                experiment_id=request.experiment_id,
                statement=request.statement,
                direction=request.direction.value,
                status=HypothesisStatus.PROPOSED.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "hypotheses.registered",
                "hypotheses",
                f"hypothesis:{request.hypothesis_id}",
                {
                    "org_id": request.org_id,
                    "experiment_id": request.experiment_id,
                    "direction": request.direction.value,
                },
            )
            return _hypothesis_from_row(row)

    def get_hypothesis(self, hypothesis_id: str, org_id: str) -> Hypothesis | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(HypothesisRecord).where(
                    HypothesisRecord.hypothesis_id == hypothesis_id,
                    HypothesisRecord.org_id == org_id,
                )
            ).first()
            return _hypothesis_from_row(row) if row is not None else None

    def list_hypotheses(self, org_id: str, experiment_id: str | None = None) -> list[Hypothesis]:
        with self._sessions() as session:
            stmt = sa.select(HypothesisRecord).where(HypothesisRecord.org_id == org_id)
            if experiment_id is not None:
                stmt = stmt.where(HypothesisRecord.experiment_id == experiment_id)
            rows = session.scalars(
                stmt.order_by(HypothesisRecord.created_at, HypothesisRecord.id)
            ).all()
            return [_hypothesis_from_row(r) for r in rows]

    def transition_hypothesis(
        self, request: TransitionRequest, org_id: str, hypothesis_id: str
    ) -> Hypothesis:
        """Apply a validated resolution (409 on illegal moves)."""
        target = HypothesisStatus(request.status)
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(HypothesisRecord).where(
                    HypothesisRecord.hypothesis_id == hypothesis_id,
                    HypothesisRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"hypothesis not found: {hypothesis_id}")
            current = HypothesisStatus(row.status)
            if not hypothesis_transition_allowed(current, target):
                raise ValueError(
                    f"illegal hypothesis transition: {current.value} -> {target.value}"
                )
            if current is not target:
                row.status = target.value
                session.flush()
                audit.record(
                    "hypotheses.status_changed",
                    "hypotheses",
                    f"hypothesis:{hypothesis_id}",
                    {
                        "org_id": org_id,
                        "from_status": current.value,
                        "to_status": target.value,
                        "actor_ref": request.actor_ref,
                        "reason_ref": request.reason_ref,
                    },
                )
            return _hypothesis_from_row(row)


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["experiments"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ExperimentStore:
    return ExperimentStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already registered" in message or "illegal" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post("/experiments", response_model=Experiment, name="register_experiment")
def register_experiment_route(
    request: RegisterExperimentRequest,
    store: Annotated[ExperimentStore, Depends(_store)],
) -> Experiment:
    """Register one experiment (idempotent per org+experiment_id)."""
    try:
        return store.register_experiment(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/experiments/{experiment_id}",
    response_model=Experiment,
    name="get_experiment",
)
def get_experiment_route(
    experiment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[ExperimentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Experiment:
    """Get one experiment by id and org."""
    stored = store.get_experiment(experiment_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"experiment not found: {experiment_id}")
    return stored


@router.get(
    "/experiments",
    response_model=list[Experiment],
    name="list_experiments",
)
def list_experiments_route(
    store: Annotated[ExperimentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    status: Annotated[ExperimentStatus | None, Query()] = None,
) -> list[Experiment]:
    """List experiments for one org, optionally filtered by status."""
    return store.list_experiments(org_id, status)


@router.post(
    "/experiments/{experiment_id}/transition",
    response_model=Experiment,
    name="transition_experiment",
)
def transition_experiment_route(
    experiment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: TransitionRequest,
    store: Annotated[ExperimentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Experiment:
    """Apply an experiment status transition (404 unknown, 409 illegal)."""
    try:
        return store.transition_experiment(request, org_id, experiment_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post("/hypotheses", response_model=Hypothesis, name="register_hypothesis")
def register_hypothesis_route(
    request: RegisterHypothesisRequest,
    store: Annotated[ExperimentStore, Depends(_store)],
) -> Hypothesis:
    """Register one hypothesis (requires linked experiment in same org)."""
    try:
        return store.register_hypothesis(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/hypotheses/{hypothesis_id}",
    response_model=Hypothesis,
    name="get_hypothesis",
)
def get_hypothesis_route(
    hypothesis_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[ExperimentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Hypothesis:
    """Get one hypothesis by id and org."""
    stored = store.get_hypothesis(hypothesis_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"hypothesis not found: {hypothesis_id}")
    return stored


@router.get(
    "/hypotheses",
    response_model=list[Hypothesis],
    name="list_hypotheses",
)
def list_hypotheses_route(
    store: Annotated[ExperimentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    experiment_id: Annotated[str | None, Query(max_length=128)] = None,
) -> list[Hypothesis]:
    """List hypotheses for one org, optionally filtered by experiment."""
    return store.list_hypotheses(org_id, experiment_id)


@router.post(
    "/hypotheses/{hypothesis_id}/transition",
    response_model=Hypothesis,
    name="transition_hypothesis",
)
def transition_hypothesis_route(
    hypothesis_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: TransitionRequest,
    store: Annotated[ExperimentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Hypothesis:
    """Apply a hypothesis resolution (404 unknown, 409 illegal)."""
    try:
        return store.transition_hypothesis(request, org_id, hypothesis_id)
    except ValueError as exc:
        raise _map_error(exc) from exc
