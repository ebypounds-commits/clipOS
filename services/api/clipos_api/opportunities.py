"""Opportunities, opportunity scores, and decisions: typed definitions and persistence.

Implements the opportunity tables of the "Signals & Opportunity" cluster:
- opportunities: one scored content opportunity derived from topics/trend data,
  org-scoped and idempotent per (org_id, opportunity_id).
- opportunity_scores: one deterministic scoring row per opportunity (pure
  weighted-sum, no LLM); re-scoring with identical components is a no-op.
- opportunity_decisions: append-only APPROVE/REJECT decision log with
  actor_ref + reason_ref (no blobs).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base


class OpportunityStatus(str, Enum):
    """Lifecycle status of one opportunity."""

    OPEN = "OPEN"
    SCORED = "SCORED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class DecisionKind(str, Enum):
    """Operator decision kinds for the append-only decision log."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"


class Opportunity(BaseModel):
    """One scored content opportunity, org-scoped, idempotent per natural key."""

    model_config = ConfigDict(frozen=True)

    opportunity_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    topic_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2048)
    status: OpportunityStatus = OpportunityStatus.OPEN
    created_at: datetime | None = None


class OpportunityScore(BaseModel):
    """One deterministic scoring row for one opportunity."""

    model_config = ConfigDict(frozen=True)

    score_id: str = Field(default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128)
    opportunity_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    components: dict[str, float] = Field(default_factory=dict)
    total: float = Field(ge=0.0, le=100.0)
    scored_at: datetime | None = None


class OpportunityDecision(BaseModel):
    """One append-only operator decision (APPROVE/REJECT) for an opportunity."""

    model_config = ConfigDict(frozen=True)

    decision_id: str = Field(
        default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128
    )
    opportunity_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    decision: DecisionKind
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = None


class OpportunityRecord(Base):
    """SQL row for one opportunity."""

    __tablename__ = "opportunities"
    __table_args__ = (
        sa.Index("uq_opportunities_org_opp", "org_id", "opportunity_id", unique=True),
        sa.Index("ix_opportunities_topic", "topic_id"),
        sa.Index("ix_opportunities_org_created", "org_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    topic_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.String(2048), nullable=True)
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class OpportunityScoreRecord(Base):
    """SQL row for one opportunity score."""

    __tablename__ = "opportunity_scores"
    __table_args__ = (
        sa.Index("uq_opportunity_scores_score_id", "score_id", unique=True),
        sa.Index("uq_opportunity_scores_opp", "opportunity_id", unique=True),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    score_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    opportunity_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    components: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default=sa.text("'{}'")
    )
    total: Mapped[float] = mapped_column(sa.Float, nullable=False)
    scored_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class OpportunityDecisionRecord(Base):
    """SQL row for one append-only decision."""

    __tablename__ = "opportunity_decisions"
    __table_args__ = (
        sa.Index("uq_opportunity_decisions_id", "decision_id", unique=True),
        sa.Index("uq_opportunity_decisions_opp_kind", "opportunity_id", "decision", unique=True),
        sa.Index("ix_opportunity_decisions_org", "org_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    opportunity_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    decision: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    actor_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    reason_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _opportunity_from_row(row: OpportunityRecord) -> Opportunity:
    return Opportunity(
        opportunity_id=row.opportunity_id,
        org_id=row.org_id,
        topic_id=row.topic_id,
        title=row.title,
        description=row.description,
        status=OpportunityStatus(row.status),
        created_at=_aware(row.created_at),
    )


def _score_from_row(row: OpportunityScoreRecord) -> OpportunityScore:
    return OpportunityScore(
        score_id=row.score_id,
        opportunity_id=row.opportunity_id,
        org_id=row.org_id,
        components=row.components or {},
        total=row.total,
        scored_at=_aware(row.scored_at),
    )


def _decision_from_row(row: OpportunityDecisionRecord) -> OpportunityDecision:
    return OpportunityDecision(
        decision_id=row.decision_id,
        opportunity_id=row.opportunity_id,
        org_id=row.org_id,
        decision=DecisionKind(row.decision),
        actor_ref=row.actor_ref,
        reason_ref=row.reason_ref,
        created_at=_aware(row.created_at),
    )


# ----------------------------------------------------------------------
# Pure deterministic scoring + policy (no LLM, no network)
# ----------------------------------------------------------------------


def score_opportunity(components: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted sum of components normalized to 0..100 (pure, deterministic).

    Each component value must be finite and in 0..1; weights must be > 0.
    Missing components count as 0. The result is clamped to 0..100.
    """
    if not weights:
        raise ValueError("weights must not be empty")
    for name, w in weights.items():
        if not (w > 0) or not math.isfinite(w):
            raise ValueError(f"weight {name!r} must be a positive finite number")
    for name, v in components.items():
        if not math.isfinite(v) or v < 0.0 or v > 1.0:
            raise ValueError(f"component {name!r} must be finite in 0..1")
    weighted = sum(weights.get(k, 0.0) * v for k, v in components.items())
    weight_total = sum(weights.values())
    if weight_total <= 0:
        raise ValueError("sum of weights must be positive")
    return round(min(100.0, max(0.0, 100.0 * weighted / weight_total)), 4)


def apply_opportunity_policy(total: float, policy: dict[str, float]) -> str:
    """Map a total score to approve/review/reject via thresholds (pure).

    The policy dict must provide ``approve_at`` and ``reject_at`` thresholds
    (0..100). total >= approve_at -> approve; total <= reject_at -> reject;
    otherwise review. Thresholds are caller-supplied, never hard-coded.
    """
    approve_at = policy.get("approve_at")
    reject_at = policy.get("reject_at")
    if approve_at is None or reject_at is None:
        raise ValueError("policy must define approve_at and reject_at")
    if not (0.0 <= reject_at <= approve_at <= 100.0):
        raise ValueError("policy thresholds must satisfy 0 <= reject_at <= approve_at <= 100")
    if total >= approve_at:
        return "approve"
    if total <= reject_at:
        return "reject"
    return "review"


class OpportunityStore:
    """Typed persistence for opportunities, scores, and decisions."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> OpportunityStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # ------------------------------------------------------------------
    # Opportunities
    # ------------------------------------------------------------------

    def register_opportunity(self, opportunity: Opportunity) -> Opportunity:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(OpportunityRecord).where(
                    OpportunityRecord.opportunity_id == opportunity.opportunity_id,
                    OpportunityRecord.org_id == opportunity.org_id,
                )
            ).first()
            if existing is not None:
                stored = _opportunity_from_row(existing)
                if (
                    stored.topic_id != opportunity.topic_id
                    or stored.title != opportunity.title
                    or stored.description != opportunity.description
                    or stored.status != opportunity.status
                ):
                    raise ValueError(
                        "opportunity already registered with different fields: "
                        f"{opportunity.opportunity_id}"
                    )
                return stored
            row = OpportunityRecord(
                opportunity_id=opportunity.opportunity_id,
                org_id=opportunity.org_id,
                topic_id=opportunity.topic_id,
                title=opportunity.title,
                description=opportunity.description,
                status=opportunity.status.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "opportunities.opportunity_registered",
                "opportunities",
                f"opportunity:{opportunity.opportunity_id}",
                {"org_id": opportunity.org_id, "topic_id": opportunity.topic_id},
            )
            return _opportunity_from_row(row)

    def get_opportunity(self, opportunity_id: str, org_id: str) -> Opportunity | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(OpportunityRecord).where(
                    OpportunityRecord.opportunity_id == opportunity_id,
                    OpportunityRecord.org_id == org_id,
                )
            ).first()
            return _opportunity_from_row(row) if row is not None else None

    def list_opportunities(
        self, org_id: str, status: OpportunityStatus | None = None
    ) -> list[Opportunity]:
        with self._sessions() as session:
            stmt = sa.select(OpportunityRecord).where(OpportunityRecord.org_id == org_id)
            if status is not None:
                stmt = stmt.where(OpportunityRecord.status == status.value)
            rows = session.scalars(stmt.order_by(OpportunityRecord.opportunity_id)).all()
            return [_opportunity_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Scores
    # ------------------------------------------------------------------

    def record_score(
        self, score: OpportunityScore, new_status: OpportunityStatus | None = None
    ) -> OpportunityScore:
        """Record one score; identical re-score no-op, different components raise."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            opp = session.scalars(
                sa.select(OpportunityRecord).where(
                    OpportunityRecord.opportunity_id == score.opportunity_id,
                    OpportunityRecord.org_id == score.org_id,
                )
            ).first()
            if opp is None:
                raise ValueError(f"opportunity not found: {score.opportunity_id}")
            existing = session.scalars(
                sa.select(OpportunityScoreRecord).where(
                    OpportunityScoreRecord.opportunity_id == score.opportunity_id
                )
            ).first()
            if existing is not None:
                stored = _score_from_row(existing)
                if stored.components != score.components or stored.total != score.total:
                    raise ValueError(
                        f"opportunity already scored with different fields: {score.opportunity_id}"
                    )
                return stored
            row = OpportunityScoreRecord(
                score_id=score.score_id,
                opportunity_id=score.opportunity_id,
                org_id=score.org_id,
                components=score.components,
                total=score.total,
                scored_at=now,
            )
            session.add(row)
            if new_status is not None:
                opp.status = new_status.value
            session.flush()
            audit.record(
                "opportunities.score_recorded",
                "opportunities",
                f"opportunity_score:{score.score_id}",
                {
                    "org_id": score.org_id,
                    "opportunity_id": score.opportunity_id,
                    "total": score.total,
                },
            )
            return _score_from_row(row)

    def get_score(self, opportunity_id: str) -> OpportunityScore | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(OpportunityScoreRecord).where(
                    OpportunityScoreRecord.opportunity_id == opportunity_id
                )
            ).first()
            return _score_from_row(row) if row is not None else None

    # ------------------------------------------------------------------
    # Decisions (append-only)
    # ------------------------------------------------------------------

    def record_decision(self, decision: OpportunityDecision) -> OpportunityDecision:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            opp = session.scalars(
                sa.select(OpportunityRecord).where(
                    OpportunityRecord.opportunity_id == decision.opportunity_id,
                    OpportunityRecord.org_id == decision.org_id,
                )
            ).first()
            if opp is None:
                raise ValueError(f"opportunity not found: {decision.opportunity_id}")
            existing = session.scalars(
                sa.select(OpportunityDecisionRecord).where(
                    OpportunityDecisionRecord.opportunity_id == decision.opportunity_id,
                    OpportunityDecisionRecord.decision == decision.decision.value,
                )
            ).first()
            if existing is not None:
                stored = _decision_from_row(existing)
                if stored.actor_ref != decision.actor_ref:
                    raise ValueError(
                        f"decision already recorded with different fields: {decision.decision_id}"
                    )
                return stored
            row = OpportunityDecisionRecord(
                decision_id=decision.decision_id,
                opportunity_id=decision.opportunity_id,
                org_id=decision.org_id,
                decision=decision.decision.value,
                actor_ref=decision.actor_ref,
                reason_ref=decision.reason_ref,
                created_at=now,
            )
            session.add(row)
            opp.status = (
                OpportunityStatus.APPROVED.value
                if decision.decision is DecisionKind.APPROVE
                else OpportunityStatus.REJECTED.value
            )
            session.flush()
            audit.record(
                "opportunities.decision_recorded",
                "opportunities",
                f"opportunity_decision:{decision.decision_id}",
                {
                    "org_id": decision.org_id,
                    "opportunity_id": decision.opportunity_id,
                    "decision": decision.decision.value,
                    "actor_ref": decision.actor_ref,
                },
            )
            return _decision_from_row(row)

    def list_decisions(self, org_id: str) -> list[OpportunityDecision]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(OpportunityDecisionRecord)
                .where(OpportunityDecisionRecord.org_id == org_id)
                .order_by(OpportunityDecisionRecord.created_at, OpportunityDecisionRecord.id)
            ).all()
            return [_decision_from_row(row) for row in rows]


# ----------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ----------------------------------------------------------------------

router = APIRouter(tags=["opportunities"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> OpportunityStore:
    return OpportunityStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


@router.post(
    "/opportunities",
    response_model=Opportunity,
    name="register_opportunity",
)
def register_opportunity_route(
    request: Opportunity,
    store: Annotated[OpportunityStore, Depends(_store)],
) -> Opportunity:
    try:
        return store.register_opportunity(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


@router.get(
    "/opportunities/{opportunity_id}",
    response_model=Opportunity,
    name="get_opportunity",
)
def get_opportunity_route(
    opportunity_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[OpportunityStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Opportunity:
    stored = store.get_opportunity(opportunity_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"opportunity not found: {opportunity_id}")
    return stored


@router.get(
    "/opportunities",
    response_model=list[Opportunity],
    name="list_opportunities",
)
def list_opportunities_route(
    store: Annotated[OpportunityStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    status: Annotated[str | None, Query()] = None,
) -> list[Opportunity]:
    st = OpportunityStatus(status) if status else None
    return store.list_opportunities(org_id, st)


@router.post(
    "/opportunities/{opportunity_id}/scores",
    response_model=OpportunityScore,
    name="record_opportunity_score",
)
def record_opportunity_score_route(
    request: OpportunityScore,
    opportunity_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[OpportunityStore, Depends(_store)],
) -> OpportunityScore:
    if request.opportunity_id != opportunity_id:
        raise HTTPException(
            status_code=422,
            detail="score opportunity_id must match the path opportunity_id",
        )
    try:
        return store.record_score(request, OpportunityStatus.SCORED)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


@router.post(
    "/opportunities/{opportunity_id}/decisions",
    response_model=OpportunityDecision,
    name="record_opportunity_decision",
)
def record_opportunity_decision_route(
    request: OpportunityDecision,
    opportunity_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[OpportunityStore, Depends(_store)],
) -> OpportunityDecision:
    if request.opportunity_id != opportunity_id:
        raise HTTPException(
            status_code=422,
            detail="decision opportunity_id must match the path opportunity_id",
        )
    try:
        return store.record_decision(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


@router.get(
    "/opportunities/{opportunity_id}/scores",
    response_model=OpportunityScore | None,
    name="get_opportunity_score",
)
def get_opportunity_score_route(
    opportunity_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[OpportunityStore, Depends(_store)],
) -> OpportunityScore | None:
    return store.get_score(opportunity_id)
