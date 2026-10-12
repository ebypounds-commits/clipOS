"""Factuality evaluations and transformation records (CLIP-049).

Closes M11 with the two evaluation artifacts from `07_SECURITY` and the
milestone list:

- **Factuality evaluations** — append-only PASS/FAIL/INCONCLUSIVE records
  against a CLIP-046 claim. Recording applies a deterministic veracity
  mapping in the same transaction: PASS -> VERIFIED, FAIL -> DISPUTED,
  INCONCLUSIVE -> unchanged (fail-safe). The mapping audits
  `claims.veracity_changed` with `reason_ref` set to the evaluation id, so
  every veracity move points at its evidence. `method_ref` is a free
  reference (manual review label, or a Model Registry alias when the
  evaluation was model-assisted) — never a hard-coded provider identifier.
- **Transformation records** — the rights-chain rows from 07:
  input ref -> output ref -> method for one content brief, append-only and
  idempotent per `(org_id, transformation_id)`.

Audits: `factuality_evaluations.recorded`, `claims.veracity_changed`,
`transformation_records.registered`. No LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.content_planning import ContentBriefRecord
from clipos_api.repository import Base
from clipos_api.research import ClaimRecord, ClaimVeracity


class EvalResult(str, Enum):
    """Outcome of one factuality evaluation."""

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


#: Deterministic mapping from evaluation result to claim veracity.
#: INCONCLUSIVE maps to None (veracity unchanged).
VERACITY_FOR_RESULT: dict[EvalResult, ClaimVeracity | None] = {
    EvalResult.PASS: ClaimVeracity.VERIFIED,
    EvalResult.FAIL: ClaimVeracity.DISPUTED,
    EvalResult.INCONCLUSIVE: None,
}


class FactualityEvaluation(BaseModel):
    """One factuality evaluation; natural key = (org_id, eval_id)."""

    model_config = ConfigDict(frozen=True)

    eval_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    claim_id: str = Field(min_length=1, max_length=128)
    result: EvalResult
    method_ref: str = Field(min_length=1, max_length=128)
    evidence_ref: str | None = Field(default=None, max_length=512)
    actor_ref: str = Field(min_length=1, max_length=255)
    created_at: datetime | None = None


class TransformationRecord(BaseModel):
    """One transformation chain row; natural key = (org_id, transformation_id)."""

    model_config = ConfigDict(frozen=True)

    transformation_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    brief_id: str = Field(min_length=1, max_length=128)
    from_ref: str = Field(min_length=1, max_length=255)
    to_ref: str = Field(min_length=1, max_length=255)
    method: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=2048)
    created_at: datetime | None = None


class FactualityEvaluationRecord(Base):
    """SQL row for one factuality evaluation."""

    __tablename__ = "factuality_evaluations"
    __table_args__ = (
        sa.Index(
            "uq_factuality_evaluations_org_eval",
            "org_id",
            "eval_id",
            unique=True,
        ),
        sa.Index("ix_factuality_evaluations_org_claim", "org_id", "claim_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    eval_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    claim_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    result: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    method_ref: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    evidence_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    actor_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class TransformationRecordRow(Base):
    """SQL row for one transformation record."""

    __tablename__ = "transformation_records"
    __table_args__ = (
        sa.Index(
            "uq_transformation_records_org_transformation",
            "org_id",
            "transformation_id",
            unique=True,
        ),
        sa.Index("ix_transformation_records_org_brief", "org_id", "brief_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    transformation_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    brief_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    from_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    to_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    method: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _evaluation_from_row(row: FactualityEvaluationRecord) -> FactualityEvaluation:
    return FactualityEvaluation(
        eval_id=row.eval_id,
        org_id=row.org_id,
        claim_id=row.claim_id,
        result=EvalResult(row.result),
        method_ref=row.method_ref,
        evidence_ref=row.evidence_ref,
        actor_ref=row.actor_ref,
        created_at=_aware(row.created_at),
    )


def _transformation_from_row(row: TransformationRecordRow) -> TransformationRecord:
    return TransformationRecord(
        transformation_id=row.transformation_id,
        org_id=row.org_id,
        brief_id=row.brief_id,
        from_ref=row.from_ref,
        to_ref=row.to_ref,
        method=row.method,
        description=row.description,
        created_at=_aware(row.created_at),
    )


class ContentEvalStore:
    """Typed persistence for factuality evaluations and transformation rows."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ContentEvalStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def record_evaluation(self, evaluation: FactualityEvaluation) -> FactualityEvaluation:
        """Record one factuality evaluation and apply its veracity mapping.

        The claim must exist org-scoped (404). Idempotent per
        (org, eval_id): identical re-record returns the stored row without
        re-applying; different fields raise (conflict). PASS maps the claim
        to VERIFIED, FAIL to DISPUTED, INCONCLUSIVE leaves veracity
        unchanged — applied atomically with the eval insert and audited as
        claims.veracity_changed with reason_ref = eval_id.
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            claim = session.scalars(
                sa.select(ClaimRecord).where(
                    ClaimRecord.claim_id == evaluation.claim_id,
                    ClaimRecord.org_id == evaluation.org_id,
                )
            ).first()
            if claim is None:
                raise ValueError(f"claim not found: {evaluation.claim_id}")
            existing = session.scalars(
                sa.select(FactualityEvaluationRecord).where(
                    FactualityEvaluationRecord.org_id == evaluation.org_id,
                    FactualityEvaluationRecord.eval_id == evaluation.eval_id,
                )
            ).first()
            if existing is not None:
                stored = _evaluation_from_row(existing)
                if (
                    stored.claim_id != evaluation.claim_id
                    or stored.result != evaluation.result
                    or stored.method_ref != evaluation.method_ref
                    or stored.evidence_ref != evaluation.evidence_ref
                    or stored.actor_ref != evaluation.actor_ref
                ):
                    raise ValueError(
                        f"evaluation already recorded with different fields: {evaluation.eval_id}"
                    )
                return stored
            row = FactualityEvaluationRecord(
                eval_id=evaluation.eval_id,
                org_id=evaluation.org_id,
                claim_id=evaluation.claim_id,
                result=evaluation.result.value,
                method_ref=evaluation.method_ref,
                evidence_ref=evaluation.evidence_ref,
                actor_ref=evaluation.actor_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "factuality_evaluations.recorded",
                "factuality_evaluations",
                f"evaluation:{evaluation.eval_id}",
                {
                    "org_id": evaluation.org_id,
                    "claim_id": evaluation.claim_id,
                    "result": evaluation.result.value,
                    "method_ref": evaluation.method_ref,
                },
            )
            target = VERACITY_FOR_RESULT[evaluation.result]
            if target is not None and claim.veracity != target.value:
                previous = claim.veracity
                claim.veracity = target.value
                session.flush()
                audit.record(
                    "claims.veracity_changed",
                    "claims",
                    f"claim:{evaluation.claim_id}",
                    {
                        "org_id": evaluation.org_id,
                        "from_status": previous,
                        "to_status": target.value,
                        "actor_ref": evaluation.actor_ref,
                        "reason_ref": evaluation.eval_id,
                    },
                )
            return _evaluation_from_row(row)

    def list_evaluations(self, org_id: str, claim_id: str) -> list[FactualityEvaluation]:
        with self._sessions() as session:
            claim = session.scalars(
                sa.select(ClaimRecord).where(
                    ClaimRecord.claim_id == claim_id,
                    ClaimRecord.org_id == org_id,
                )
            ).first()
            if claim is None:
                raise ValueError(f"claim not found: {claim_id}")
            rows = session.scalars(
                sa.select(FactualityEvaluationRecord)
                .where(
                    FactualityEvaluationRecord.org_id == org_id,
                    FactualityEvaluationRecord.claim_id == claim_id,
                )
                .order_by(FactualityEvaluationRecord.created_at, FactualityEvaluationRecord.id)
            ).all()
            return [_evaluation_from_row(row) for row in rows]

    def register_transformation(self, record: TransformationRecord) -> TransformationRecord:
        """Idempotent register per (org, transformation_id); brief must exist."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            brief = session.scalars(
                sa.select(ContentBriefRecord).where(
                    ContentBriefRecord.brief_id == record.brief_id,
                    ContentBriefRecord.org_id == record.org_id,
                )
            ).first()
            if brief is None:
                raise ValueError(f"content brief not found: {record.brief_id}")
            existing = session.scalars(
                sa.select(TransformationRecordRow).where(
                    TransformationRecordRow.org_id == record.org_id,
                    TransformationRecordRow.transformation_id == record.transformation_id,
                )
            ).first()
            if existing is not None:
                stored = _transformation_from_row(existing)
                if (
                    stored.brief_id != record.brief_id
                    or stored.from_ref != record.from_ref
                    or stored.to_ref != record.to_ref
                    or stored.method != record.method
                    or stored.description != record.description
                ):
                    raise ValueError(
                        f"transformation already recorded with different fields: "
                        f"{record.transformation_id}"
                    )
                return stored
            row = TransformationRecordRow(
                transformation_id=record.transformation_id,
                org_id=record.org_id,
                brief_id=record.brief_id,
                from_ref=record.from_ref,
                to_ref=record.to_ref,
                method=record.method,
                description=record.description,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "transformation_records.registered",
                "transformation_records",
                f"transformation:{record.transformation_id}",
                {
                    "org_id": record.org_id,
                    "brief_id": record.brief_id,
                    "method": record.method,
                },
            )
            return _transformation_from_row(row)

    def list_transformations(self, org_id: str, brief_id: str) -> list[TransformationRecord]:
        with self._sessions() as session:
            brief = session.scalars(
                sa.select(ContentBriefRecord).where(
                    ContentBriefRecord.brief_id == brief_id,
                    ContentBriefRecord.org_id == org_id,
                )
            ).first()
            if brief is None:
                raise ValueError(f"content brief not found: {brief_id}")
            rows = session.scalars(
                sa.select(TransformationRecordRow)
                .where(
                    TransformationRecordRow.org_id == org_id,
                    TransformationRecordRow.brief_id == brief_id,
                )
                .order_by(TransformationRecordRow.created_at, TransformationRecordRow.id)
            ).all()
            return [_transformation_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["content-evals"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ContentEvalStore:
    return ContentEvalStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        status = 404
    elif "already" in message:
        status = 409
    else:
        status = 422
    return HTTPException(status_code=status, detail=message)


@router.post(
    "/factuality-evaluations",
    response_model=FactualityEvaluation,
    name="record_factuality_evaluation",
)
def record_factuality_evaluation_route(
    request: FactualityEvaluation,
    store: Annotated[ContentEvalStore, Depends(_store)],
) -> FactualityEvaluation:
    """Record one factuality evaluation and apply its veracity mapping."""
    try:
        return store.record_evaluation(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/factuality-evaluations",
    response_model=list[FactualityEvaluation],
    name="list_factuality_evaluations",
)
def list_factuality_evaluations_route(
    store: Annotated[ContentEvalStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    claim_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> list[FactualityEvaluation]:
    """List evaluations for one claim (404 when the claim is unknown)."""
    try:
        return store.list_evaluations(org_id, claim_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/transformation-records",
    response_model=TransformationRecord,
    name="register_transformation_record",
)
def register_transformation_record_route(
    request: TransformationRecord,
    store: Annotated[ContentEvalStore, Depends(_store)],
) -> TransformationRecord:
    """Register one rights-chain transformation row (idempotent)."""
    try:
        return store.register_transformation(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/transformation-records",
    response_model=list[TransformationRecord],
    name="list_transformation_records",
)
def list_transformation_records_route(
    store: Annotated[ContentEvalStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    brief_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> list[TransformationRecord]:
    """List transformation rows for one brief (404 when unknown)."""
    try:
        return store.list_transformations(org_id, brief_id)
    except ValueError as exc:
        raise _map_error(exc) from exc
