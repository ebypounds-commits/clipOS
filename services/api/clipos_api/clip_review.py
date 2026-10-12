"""Review decisions for clip candidates (CLIP-045).

Append-only APPROVE/REJECT decision log with idempotent recording — the
operator review half of M10 review tooling. Recording a decision transitions
the candidate to APPROVED/REJECTED (CLIP-043 scoring freezes decided
candidates). Identical re-record with the same actor is a no-op; a different
actor for the same decision kind raises (audit integrity). Mirrors the
CLIP-036 `opportunity_decisions` contract. No LLM, no network, no provider
identifiers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.clip_candidates import ClipCandidateRecord, ClipCandidateStatus
from clipos_api.repository import Base


class DecisionKind(str, Enum):
    """Operator review decision kinds (append-only)."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"


class CandidateDecision(BaseModel):
    """One review decision for one clip candidate."""

    model_config = ConfigDict(frozen=True)

    decision_id: str = Field(
        default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128
    )
    candidate_id: str = Field(min_length=1, max_length=36)
    org_id: str = Field(min_length=1, max_length=64)
    decision: DecisionKind
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = None


class DecisionRequest(BaseModel):
    """Request body for recording one review decision."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    decision: DecisionKind
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)


class CandidateDecisionRecord(Base):
    """SQL row for one append-only review decision."""

    __tablename__ = "candidate_decisions"
    __table_args__ = (
        sa.Index("uq_candidate_decisions_id", "decision_id", unique=True),
        sa.Index(
            "uq_candidate_decisions_candidate_decision",
            "candidate_id",
            "decision",
            unique=True,
        ),
        sa.Index("ix_candidate_decisions_org", "org_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    candidate_id: Mapped[str] = mapped_column(sa.String(36), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    decision: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    actor_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    reason_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _decision_from_row(row: CandidateDecisionRecord) -> CandidateDecision:
    return CandidateDecision(
        decision_id=row.decision_id,
        candidate_id=row.candidate_id,
        org_id=row.org_id,
        decision=DecisionKind(row.decision),
        actor_ref=row.actor_ref,
        reason_ref=row.reason_ref,
        created_at=_aware(row.created_at),
    )


class ClipReviewStore:
    """Typed persistence for candidate review decisions."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ClipReviewStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def record_decision(self, decision: CandidateDecision) -> CandidateDecision:
        """Record one decision idempotently; transition candidate status.

        Identical re-record (same kind, same actor) returns the stored row;
        a different actor for the same kind raises (conflict). APPROVE sets
        candidate status APPROVED, REJECT sets REJECTED. Missing candidate
        raises ValueError (route maps to 404).
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            candidate = session.scalars(
                sa.select(ClipCandidateRecord).where(
                    ClipCandidateRecord.candidate_id == decision.candidate_id,
                    ClipCandidateRecord.org_id == decision.org_id,
                )
            ).first()
            if candidate is None:
                raise ValueError(f"clip candidate not found: {decision.candidate_id}")
            existing = session.scalars(
                sa.select(CandidateDecisionRecord).where(
                    CandidateDecisionRecord.candidate_id == decision.candidate_id,
                    CandidateDecisionRecord.decision == decision.decision.value,
                )
            ).first()
            if existing is not None:
                stored = _decision_from_row(existing)
                if stored.actor_ref != decision.actor_ref:
                    raise ValueError(
                        f"decision already recorded with different fields: "
                        f"{decision.candidate_id} decision={decision.decision.value}"
                    )
                return stored
            row = CandidateDecisionRecord(
                decision_id=decision.decision_id,
                candidate_id=decision.candidate_id,
                org_id=decision.org_id,
                decision=decision.decision.value,
                actor_ref=decision.actor_ref,
                reason_ref=decision.reason_ref,
                created_at=now,
            )
            session.add(row)
            candidate.status = (
                ClipCandidateStatus.APPROVED.value
                if decision.decision is DecisionKind.APPROVE
                else ClipCandidateStatus.REJECTED.value
            )
            session.flush()
            audit.record(
                "clip_candidates.decided",
                "clip_candidates",
                f"candidate:{decision.candidate_id}",
                {
                    "org_id": decision.org_id,
                    "decision": decision.decision.value,
                    "actor_ref": decision.actor_ref,
                    "has_reason": decision.reason_ref is not None,
                },
            )
            return _decision_from_row(row)

    def list_decisions(self, candidate_id: str, org_id: str) -> list[CandidateDecision]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(CandidateDecisionRecord)
                .where(
                    CandidateDecisionRecord.org_id == org_id,
                    CandidateDecisionRecord.candidate_id == candidate_id,
                )
                .order_by(CandidateDecisionRecord.created_at, CandidateDecisionRecord.id)
            ).all()
            return [_decision_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["clip-review"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ClipReviewStore:
    return ClipReviewStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


@router.post(
    "/clip-candidates/{candidate_id}/decisions",
    response_model=CandidateDecision,
    name="record_clip_decision",
)
def record_clip_decision_route(
    request: DecisionRequest,
    candidate_id: Annotated[str, Path(min_length=1, max_length=36)],
    store: Annotated[ClipReviewStore, Depends(_store)],
) -> CandidateDecision:
    """Record an APPROVE/REJECT decision and transition candidate status."""
    try:
        return store.record_decision(
            CandidateDecision(
                candidate_id=candidate_id,
                org_id=request.org_id,
                decision=request.decision,
                actor_ref=request.actor_ref,
                reason_ref=request.reason_ref,
            )
        )
    except ValueError as exc:
        message = str(exc)
        if "not found" in message:
            status = 404
        elif "already recorded with different" in message:
            status = 409
        else:
            status = 422
        raise HTTPException(status_code=status, detail=message) from exc


@router.get(
    "/clip-candidates/{candidate_id}/decisions",
    response_model=list[CandidateDecision],
    name="list_clip_decisions",
)
def list_clip_decisions_route(
    candidate_id: Annotated[str, Path(min_length=1, max_length=36)],
    store: Annotated[ClipReviewStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[CandidateDecision]:
    """List append-only decisions for one candidate (empty when none)."""
    return store.list_decisions(candidate_id, org_id)
