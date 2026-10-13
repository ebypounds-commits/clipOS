"""Deterministic assignment/variant orchestration (CLIP-067).

Composes the CLIP-066 experiment records with deterministic, reproducible
subject->variant assignment (PRD §H experiments):

- **Variants** — `experiment_variants` rows are registered only while
  the experiment is DRAFT (frozen once ACTIVE), idempotent per
  (org, experiment_id, variant_key); changed label/weight conflict
  (409). Each variant carries an integer weight (1..100); weights define
  the bucket boundaries.
- **Deterministic assignment** — `select_variant` is pure: bucket =
  sha256(org|experiment|subject) mod total_weight, then cumulative-weight
  selection (ties resolve to the first matching variant). The same
  subject always maps to the same variant while weights are unchanged —
  no RNG, no wall clock, no network.
- **Persisted stability** — assignments are stored once per
  (org, experiment_id, subject_ref); an existing assignment is returned
  as-is, so later weight edits or re-computations NEVER move a subject.
  Requires the experiment ACTIVE and at least two variants (else 409).
  Audited `experiment_assignments.assigned`.
- **Summary** — variant-summary counts assignments per variant (pure
  aggregation).

Migration 0045 (experiment_variants + experiment_assignments).
Routes (CLIP-033 pattern): POST/GET /experiments/{id}/variants, POST/
GET /experiments/{id}/assignments, GET /experiments/{id}/variant-summary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN, ExperimentRecord, ExperimentStatus
from clipos_api.repository import Base


class ExperimentVariant(BaseModel):
    """One weighted variant of an experiment."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    experiment_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    variant_key: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    label: str = Field(min_length=1, max_length=128)
    weight: int = Field(ge=1, le=100)
    created_at: datetime | None = None


class RegisterVariantRequest(BaseModel):
    """Request body for registering one variant."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    variant_key: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    label: str = Field(min_length=1, max_length=128)
    weight: int = Field(ge=1, le=100)


class Assignment(BaseModel):
    """One persisted subject->variant assignment."""

    model_config = ConfigDict(frozen=True)

    assignment_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    experiment_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    subject_ref: str = Field(min_length=1, max_length=255)
    variant_key: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    created_at: datetime | None = None


class AssignRequest(BaseModel):
    """Request body for assigning one subject."""

    model_config = ConfigDict(frozen=True)

    subject_ref: str = Field(min_length=1, max_length=255)


class VariantSummary(BaseModel):
    """Assignment counts per variant for one experiment."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    experiment_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    total_assigned: int = Field(ge=0)
    counts: dict[str, int] = Field(default_factory=dict)


class ExperimentVariantRecord(Base):
    """SQL row for one experiment variant."""

    __tablename__ = "experiment_variants"
    __table_args__ = (
        sa.Index(
            "uq_experiment_variants_identity",
            "org_id",
            "experiment_id",
            "variant_key",
            unique=True,
        ),
        sa.Index("ix_experiment_variants_org_experiment", "org_id", "experiment_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    variant_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    label: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    weight: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ExperimentAssignmentRecord(Base):
    """SQL row for one persisted subject assignment."""

    __tablename__ = "experiment_assignments"
    __table_args__ = (
        sa.Index(
            "uq_experiment_assignments_identity",
            "org_id",
            "experiment_id",
            "subject_ref",
            unique=True,
        ),
        sa.Index("ix_experiment_assignments_org_variant", "org_id", "experiment_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    assignment_id: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    subject_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    variant_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def deterministic_bucket(org_id: str, experiment_id: str, subject_ref: str, modulus: int) -> int:
    """Pure: sha256-based stable bucket in [0, modulus)."""
    if modulus <= 0:
        raise ValueError("modulus must be positive")
    material = f"{org_id}|{experiment_id}|{subject_ref}"
    digest = int(sha256(material.encode()).hexdigest(), 16)
    return digest % modulus


def select_variant(
    variants: list[ExperimentVariant],
    org_id: str,
    experiment_id: str,
    subject_ref: str,
) -> str:
    """Pure deterministic assignment via cumulative weights.

    bucket = sha256(org|experiment|subject) mod sum(weights); the first
    variant whose cumulative weight range contains the bucket wins.
    """
    if not variants:
        raise ValueError("experiment has no variants")
    ordered = sorted(variants, key=lambda v: v.variant_key)
    total = sum(v.weight for v in ordered)
    bucket = deterministic_bucket(org_id, experiment_id, subject_ref, total)
    cumulative = 0
    for variant in ordered:
        cumulative += variant.weight
        if bucket < cumulative:
            return variant.variant_key
    return ordered[-1].variant_key


def _variant_from_row(row: ExperimentVariantRecord) -> ExperimentVariant:
    return ExperimentVariant(
        org_id=row.org_id,
        experiment_id=row.experiment_id,
        variant_key=row.variant_key,
        label=row.label,
        weight=row.weight,
        created_at=_aware(row.created_at),
    )


def _assignment_from_row(row: ExperimentAssignmentRecord) -> Assignment:
    return Assignment(
        assignment_id=row.assignment_id,
        org_id=row.org_id,
        experiment_id=row.experiment_id,
        subject_ref=row.subject_ref,
        variant_key=row.variant_key,
        created_at=_aware(row.created_at),
    )


class OrchestrationStore:
    """Typed persistence for variants and assignments."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> OrchestrationStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_variant(
        self, experiment_id: str, request: RegisterVariantRequest
    ) -> ExperimentVariant:
        """Idempotent per (org, experiment, variant_key); DRAFT-only."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            experiment = session.scalars(
                sa.select(ExperimentRecord).where(
                    ExperimentRecord.org_id == request.org_id,
                    ExperimentRecord.experiment_id == experiment_id,
                )
            ).first()
            if experiment is None:
                raise ValueError(f"experiment not found: {experiment_id}")
            status = ExperimentStatus(experiment.status)
            if status is not ExperimentStatus.DRAFT:
                raise ValueError(f"variants can only be registered in DRAFT: {experiment_id}")
            existing = session.scalars(
                sa.select(ExperimentVariantRecord).where(
                    ExperimentVariantRecord.org_id == request.org_id,
                    ExperimentVariantRecord.experiment_id == experiment_id,
                    ExperimentVariantRecord.variant_key == request.variant_key,
                )
            ).first()
            if existing is not None:
                stored = _variant_from_row(existing)
                if stored.label != request.label or stored.weight != request.weight:
                    raise ValueError(
                        f"variant already registered with different fields: {request.variant_key}"
                    )
                return stored
            row = ExperimentVariantRecord(
                org_id=request.org_id,
                experiment_id=experiment_id,
                variant_key=request.variant_key,
                label=request.label,
                weight=request.weight,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "experiment_variants.registered",
                "experiment_variants",
                f"experiment_variant:{experiment_id}:{request.variant_key}",
                {
                    "org_id": request.org_id,
                    "experiment_id": experiment_id,
                    "variant_key": request.variant_key,
                    "weight": request.weight,
                },
            )
            return _variant_from_row(row)

    def list_variants(self, org_id: str, experiment_id: str) -> list[ExperimentVariant]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ExperimentVariantRecord)
                .where(
                    ExperimentVariantRecord.org_id == org_id,
                    ExperimentVariantRecord.experiment_id == experiment_id,
                )
                .order_by(ExperimentVariantRecord.variant_key)
            ).all()
            return [_variant_from_row(r) for r in rows]

    def assign(self, experiment_id: str, request: AssignRequest, org_id: str) -> Assignment:
        """Deterministic, persisted, idempotent assignment (ACTIVE only).

        Existing assignments return unchanged (weights never move a
        stored subject). First assignment requires >= 2 variants.
        """
        now = datetime.now(UTC)
        # Fast path: existing assignment.
        with self._sessions() as session:
            existing = session.scalars(
                sa.select(ExperimentAssignmentRecord).where(
                    ExperimentAssignmentRecord.org_id == org_id,
                    ExperimentAssignmentRecord.experiment_id == experiment_id,
                    ExperimentAssignmentRecord.subject_ref == request.subject_ref,
                )
            ).first()
            if existing is not None:
                return _assignment_from_row(existing)
            experiment = session.scalars(
                sa.select(ExperimentRecord).where(
                    ExperimentRecord.org_id == org_id,
                    ExperimentRecord.experiment_id == experiment_id,
                )
            ).first()
            if experiment is None:
                raise ValueError(f"experiment not found: {experiment_id}")
            if ExperimentStatus(experiment.status) is not ExperimentStatus.ACTIVE:
                raise ValueError(f"experiment must be ACTIVE to assign: {experiment_id}")
            variant_rows = session.scalars(
                sa.select(ExperimentVariantRecord).where(
                    ExperimentVariantRecord.org_id == org_id,
                    ExperimentVariantRecord.experiment_id == experiment_id,
                )
            ).all()
        variants = [_variant_from_row(r) for r in variant_rows]
        if len(variants) < 2:
            raise ValueError(f"experiment requires at least two variants: {experiment_id}")
        variant_key = select_variant(variants, org_id, experiment_id, request.subject_ref)
        assignment_id = str(uuid4())[:12]
        with self._sessions.begin() as session:
            raced = session.scalars(
                sa.select(ExperimentAssignmentRecord).where(
                    ExperimentAssignmentRecord.org_id == org_id,
                    ExperimentAssignmentRecord.experiment_id == experiment_id,
                    ExperimentAssignmentRecord.subject_ref == request.subject_ref,
                )
            ).first()
            if raced is not None:
                return _assignment_from_row(raced)
            row = ExperimentAssignmentRecord(
                assignment_id=assignment_id,
                org_id=org_id,
                experiment_id=experiment_id,
                subject_ref=request.subject_ref,
                variant_key=variant_key,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "experiment_assignments.assigned",
                "experiment_assignments",
                f"experiment_assignment:{assignment_id}",
                {
                    "org_id": org_id,
                    "experiment_id": experiment_id,
                    "variant_key": variant_key,
                },
            )
            return _assignment_from_row(row)

    def list_assignments(self, org_id: str, experiment_id: str) -> list[Assignment]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ExperimentAssignmentRecord)
                .where(
                    ExperimentAssignmentRecord.org_id == org_id,
                    ExperimentAssignmentRecord.experiment_id == experiment_id,
                )
                .order_by(ExperimentAssignmentRecord.created_at)
            ).all()
            return [_assignment_from_row(r) for r in rows]

    def variant_summary(self, org_id: str, experiment_id: str) -> VariantSummary:
        """Pure aggregation of persisted assignment counts."""
        assignments = self.list_assignments(org_id, experiment_id)
        counts: dict[str, int] = {}
        for assignment in assignments:
            counts[assignment.variant_key] = counts.get(assignment.variant_key, 0) + 1
        return VariantSummary(
            org_id=org_id,
            experiment_id=experiment_id,
            total_assigned=len(assignments),
            counts=counts,
        )


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["experiments"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> OrchestrationStore:
    return OrchestrationStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if (
        "already registered" in message
        or "must be ACTIVE" in message
        or "at least two variants" in message
        or "only be registered" in message
    ):
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post(
    "/experiments/{experiment_id}/variants",
    response_model=ExperimentVariant,
    name="register_experiment_variant",
)
def register_experiment_variant_route(
    experiment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: RegisterVariantRequest,
    store: Annotated[OrchestrationStore, Depends(_store)],
) -> ExperimentVariant:
    """Register one weighted variant (DRAFT-only, idempotent)."""
    try:
        return store.register_variant(experiment_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/experiments/{experiment_id}/variants",
    response_model=list[ExperimentVariant],
    name="list_experiment_variants",
)
def list_experiment_variants_route(
    experiment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[OrchestrationStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[ExperimentVariant]:
    """List variants for one experiment."""
    return store.list_variants(org_id, experiment_id)


@router.post(
    "/experiments/{experiment_id}/assignments",
    response_model=Assignment,
    name="assign_experiment_variant",
)
def assign_experiment_variant_route(
    experiment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: AssignRequest,
    store: Annotated[OrchestrationStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Assignment:
    """Deterministically assign one subject (idempotent, ACTIVE only)."""
    try:
        return store.assign(experiment_id, request, org_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/experiments/{experiment_id}/assignments",
    response_model=list[Assignment],
    name="list_experiment_assignments",
)
def list_experiment_assignments_route(
    experiment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[OrchestrationStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[Assignment]:
    """List persisted assignments for one experiment."""
    return store.list_assignments(org_id, experiment_id)


@router.get(
    "/experiments/{experiment_id}/variant-summary",
    response_model=VariantSummary,
    name="experiment_variant_summary",
)
def experiment_variant_summary_route(
    experiment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[OrchestrationStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> VariantSummary:
    """Assignment counts per variant for one experiment."""
    return store.variant_summary(org_id, experiment_id)
