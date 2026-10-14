"""Eval sandbox and safe promotion (CLIP-084).

PRD journey 5 tail — evaluate an accepted improvement inside an
isolated sandbox, then promote only when the evidence says so:

- **Sandboxes** — `eval_sandboxes` rows are org-scoped, idempotent
  per (org_id, sandbox_id) with slug ids; each binds one CLIP-082
  proposal (must exist in the same org, 404 otherwise), a required
  `evaluator_alias` (Model/Eval Registry reference — never defaulted,
  never a vendor identifier), and an opaque scenario_ref. Lifecycle
  (pure `sandbox_transition_allowed`): CREATED -> RUNNING ->
  PASSED or FAILED; FAILED -> RUNNING (re-run); PASSED and FAILED are
  otherwise terminal for promotion purposes; same-state no-op;
  anything else 409. Audited `eval_sandboxes.registered` and
  `eval_sandboxes.status_changed` (actor/reason refs).
- **Safe promotion** — `evaluate_promotion(sandbox_status,
  proposal_status)` is the pure gate: allowed only when the sandbox
  is PASSED and the proposal is ACCEPTED or IMPLEMENTED (reasons
  `sandbox_not_passed` / `proposal_not_ready` / `allowed`). Every
  promotion request persists a `promotions` row carrying the decision
  (PROMOTED or DENIED with reason) — request idempotent per
  (org, promotion_id), conflict on changed inputs (409); proposal and
  sandbox existence enforced (404); audited
  `promotions.evaluated`.

Migration 0061 (CREATE-only). Routes (CLIP-033 pattern): POST/GET
/eval-sandboxes, POST /eval-sandboxes/{sandbox_id}/transition,
POST/GET /promotions. No LLM, no network.
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
from clipos_api.improvement import (
    ImprovementProposalRecord,
    ProposalStatus,
)
from clipos_api.repository import Base


class SandboxStatus(str, Enum):
    """Eval sandbox lifecycle."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"


SANDBOX_TRANSITIONS: dict[SandboxStatus, frozenset[SandboxStatus]] = {
    SandboxStatus.CREATED: frozenset({SandboxStatus.RUNNING}),
    SandboxStatus.RUNNING: frozenset({SandboxStatus.PASSED, SandboxStatus.FAILED}),
    SandboxStatus.FAILED: frozenset({SandboxStatus.RUNNING}),
    SandboxStatus.PASSED: frozenset(),
}


def sandbox_transition_allowed(current: SandboxStatus, target: SandboxStatus) -> bool:
    """Pure: legal sandbox move (same-state counts as no-op)."""
    if current is target:
        return True
    return target in SANDBOX_TRANSITIONS[current]


class PromotionDecision(BaseModel):
    """Pure safe-promotion gate output."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str = Field(min_length=1, max_length=32)
    sandbox_status: str = Field(min_length=1, max_length=16)
    proposal_status: str = Field(min_length=1, max_length=16)


def evaluate_promotion(
    sandbox_status: SandboxStatus, proposal_status: ProposalStatus
) -> PromotionDecision:
    """Pure gate: PASSED sandbox + ACCEPTED/IMPLEMENTED proposal."""
    if sandbox_status is not SandboxStatus.PASSED:
        return PromotionDecision(
            allowed=False,
            reason="sandbox_not_passed",
            sandbox_status=sandbox_status.value,
            proposal_status=proposal_status.value,
        )
    if proposal_status not in (ProposalStatus.ACCEPTED, ProposalStatus.IMPLEMENTED):
        return PromotionDecision(
            allowed=False,
            reason="proposal_not_ready",
            sandbox_status=sandbox_status.value,
            proposal_status=proposal_status.value,
        )
    return PromotionDecision(
        allowed=True,
        reason="allowed",
        sandbox_status=sandbox_status.value,
        proposal_status=proposal_status.value,
    )


class EvalSandbox(BaseModel):
    """One eval sandbox for one proposal."""

    model_config = ConfigDict(frozen=True)

    sandbox_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    evaluator_alias: str = Field(min_length=1, max_length=128)
    scenario_ref: str = Field(min_length=1, max_length=512)
    status: SandboxStatus = SandboxStatus.CREATED
    created_at: datetime | None = None


class Promotion(BaseModel):
    """One persisted promotion decision."""

    model_config = ConfigDict(frozen=True)

    promotion_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    sandbox_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    allowed: bool
    reason: str = Field(min_length=1, max_length=32)
    actor_ref: str = Field(min_length=1, max_length=255)
    created_at: datetime | None = None


class RegisterSandboxRequest(BaseModel):
    """Request body for creating one sandbox."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    sandbox_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    proposal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    evaluator_alias: str = Field(min_length=1, max_length=128)
    scenario_ref: str = Field(min_length=1, max_length=512)


class SandboxTransitionRequest(BaseModel):
    """Request body for one sandbox transition."""

    model_config = ConfigDict(frozen=True)

    status: SandboxStatus
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)


class PromotionRequest(BaseModel):
    """Request body for one promotion evaluation."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    promotion_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    proposal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    sandbox_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    actor_ref: str = Field(min_length=1, max_length=255)


class EvalSandboxRecord(Base):
    """SQL row for one eval sandbox."""

    __tablename__ = "eval_sandboxes"
    __table_args__ = (
        sa.Index(
            "uq_eval_sandboxes_org_sandbox",
            "org_id",
            "sandbox_id",
            unique=True,
        ),
        sa.Index("ix_eval_sandboxes_org_proposal", "org_id", "proposal_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    sandbox_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    proposal_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    evaluator_alias: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    scenario_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class PromotionRecord(Base):
    """SQL row for one promotion decision."""

    __tablename__ = "promotions"
    __table_args__ = (
        sa.Index("uq_promotions_org_promotion", "org_id", "promotion_id", unique=True),
        sa.Index("ix_promotions_org_proposal", "org_id", "proposal_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    promotion_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    proposal_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    sandbox_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    allowed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    actor_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _sandbox_from_row(row: EvalSandboxRecord) -> EvalSandbox:
    return EvalSandbox(
        sandbox_id=row.sandbox_id,
        org_id=row.org_id,
        proposal_id=row.proposal_id,
        evaluator_alias=row.evaluator_alias,
        scenario_ref=row.scenario_ref,
        status=SandboxStatus(row.status),
        created_at=_aware(row.created_at),
    )


def _promotion_from_row(row: PromotionRecord) -> Promotion:
    return Promotion(
        promotion_id=row.promotion_id,
        org_id=row.org_id,
        proposal_id=row.proposal_id,
        sandbox_id=row.sandbox_id,
        allowed=row.allowed,
        reason=row.reason,
        actor_ref=row.actor_ref,
        created_at=_aware(row.created_at),
    )


class EvalStore:
    """Typed persistence for sandboxes and promotions."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> EvalStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_sandbox(self, request: RegisterSandboxRequest) -> EvalSandbox:
        """Idempotent per (org, sandbox_id); proposal must exist (404)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            proposal = session.scalars(
                sa.select(ImprovementProposalRecord).where(
                    ImprovementProposalRecord.org_id == request.org_id,
                    ImprovementProposalRecord.proposal_id == request.proposal_id,
                )
            ).first()
            if proposal is None:
                raise ValueError(f"improvement proposal not found: {request.proposal_id}")
            existing = session.scalars(
                sa.select(EvalSandboxRecord).where(
                    EvalSandboxRecord.org_id == request.org_id,
                    EvalSandboxRecord.sandbox_id == request.sandbox_id,
                )
            ).first()
            if existing is not None:
                stored = _sandbox_from_row(existing)
                if (
                    stored.proposal_id != request.proposal_id
                    or stored.evaluator_alias != request.evaluator_alias
                    or stored.scenario_ref != request.scenario_ref
                ):
                    raise ValueError(
                        f"eval sandbox already registered with different fields: "
                        f"{request.sandbox_id}"
                    )
                return stored
            row = EvalSandboxRecord(
                sandbox_id=request.sandbox_id,
                org_id=request.org_id,
                proposal_id=request.proposal_id,
                evaluator_alias=request.evaluator_alias,
                scenario_ref=request.scenario_ref,
                status=SandboxStatus.CREATED.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "eval_sandboxes.registered",
                "eval_sandboxes",
                f"eval_sandbox:{request.sandbox_id}",
                {
                    "org_id": request.org_id,
                    "proposal_id": request.proposal_id,
                    "evaluator_alias": request.evaluator_alias,
                },
            )
            return _sandbox_from_row(row)

    def list_sandboxes(self, org_id: str, status: SandboxStatus | None = None) -> list[EvalSandbox]:
        with self._sessions() as session:
            stmt = sa.select(EvalSandboxRecord).where(EvalSandboxRecord.org_id == org_id)
            if status is not None:
                stmt = stmt.where(EvalSandboxRecord.status == status.value)
            rows = session.scalars(
                stmt.order_by(EvalSandboxRecord.created_at, EvalSandboxRecord.id)
            ).all()
            return [_sandbox_from_row(r) for r in rows]

    def transition_sandbox(
        self, sandbox_id: str, org_id: str, request: SandboxTransitionRequest
    ) -> EvalSandbox:
        """Apply a validated sandbox move (404 unknown, 409 illegal)."""
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(EvalSandboxRecord).where(
                    EvalSandboxRecord.sandbox_id == sandbox_id,
                    EvalSandboxRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"eval sandbox not found: {sandbox_id}")
            current = SandboxStatus(row.status)
            target = request.status
            if not sandbox_transition_allowed(current, target):
                raise ValueError(f"illegal sandbox transition: {current.value} -> {target.value}")
            if current is not target:
                row.status = target.value
                session.flush()
                audit.record(
                    "eval_sandboxes.status_changed",
                    "eval_sandboxes",
                    f"eval_sandbox:{sandbox_id}",
                    {
                        "org_id": org_id,
                        "from_status": current.value,
                        "to_status": target.value,
                        "actor_ref": request.actor_ref,
                        "reason_ref": request.reason_ref,
                    },
                )
            return _sandbox_from_row(row)

    def evaluate_and_store_promotion(self, request: PromotionRequest) -> Promotion:
        """Gate one promotion attempt and persist the decision.

        Proposal and sandbox must exist in the org (404); identical
        (org, promotion_id) replays, changed inputs conflict (409).
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(PromotionRecord).where(
                    PromotionRecord.org_id == request.org_id,
                    PromotionRecord.promotion_id == request.promotion_id,
                )
            ).first()
            if existing is not None:
                stored = _promotion_from_row(existing)
                if (
                    stored.proposal_id != request.proposal_id
                    or stored.sandbox_id != request.sandbox_id
                    or stored.actor_ref != request.actor_ref
                ):
                    raise ValueError(
                        f"promotion already recorded with different fields: {request.promotion_id}"
                    )
                return stored
            proposal = session.scalars(
                sa.select(ImprovementProposalRecord).where(
                    ImprovementProposalRecord.org_id == request.org_id,
                    ImprovementProposalRecord.proposal_id == request.proposal_id,
                )
            ).first()
            if proposal is None:
                raise ValueError(f"improvement proposal not found: {request.proposal_id}")
            sandbox = session.scalars(
                sa.select(EvalSandboxRecord).where(
                    EvalSandboxRecord.org_id == request.org_id,
                    EvalSandboxRecord.sandbox_id == request.sandbox_id,
                )
            ).first()
            if sandbox is None:
                raise ValueError(f"eval sandbox not found: {request.sandbox_id}")
            decision = evaluate_promotion(
                SandboxStatus(sandbox.status), ProposalStatus(proposal.status)
            )
            row = PromotionRecord(
                promotion_id=request.promotion_id,
                org_id=request.org_id,
                proposal_id=request.proposal_id,
                sandbox_id=request.sandbox_id,
                allowed=decision.allowed,
                reason=decision.reason,
                actor_ref=request.actor_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "promotions.evaluated",
                "promotions",
                f"promotion:{request.promotion_id}",
                {
                    "org_id": request.org_id,
                    "proposal_id": request.proposal_id,
                    "sandbox_id": request.sandbox_id,
                    "allowed": decision.allowed,
                    "reason": decision.reason,
                },
            )
            return _promotion_from_row(row)

    def list_promotions(self, org_id: str) -> list[Promotion]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(PromotionRecord)
                .where(PromotionRecord.org_id == org_id)
                .order_by(PromotionRecord.created_at, PromotionRecord.id)
            ).all()
            return [_promotion_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["eval-promotion"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> EvalStore:
    return EvalStore.from_engine(engine)


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


@router.post("/eval-sandboxes", response_model=EvalSandbox, name="register_eval_sandbox")
def register_eval_sandbox_route(
    request: RegisterSandboxRequest,
    store: Annotated[EvalStore, Depends(_store)],
) -> EvalSandbox:
    """Create one eval sandbox (proposal must exist; idempotent)."""
    try:
        return store.register_sandbox(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/eval-sandboxes",
    response_model=list[EvalSandbox],
    name="list_eval_sandboxes",
)
def list_eval_sandboxes_route(
    store: Annotated[EvalStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    status: Annotated[SandboxStatus | None, Query()] = None,
) -> list[EvalSandbox]:
    """List eval sandboxes for one org."""
    return store.list_sandboxes(org_id, status)


@router.post(
    "/eval-sandboxes/{sandbox_id}/transition",
    response_model=EvalSandbox,
    name="transition_eval_sandbox",
)
def transition_eval_sandbox_route(
    sandbox_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: SandboxTransitionRequest,
    store: Annotated[EvalStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> EvalSandbox:
    """Apply a sandbox transition (404 unknown, 409 illegal)."""
    try:
        return store.transition_sandbox(sandbox_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post("/promotions", response_model=Promotion, name="evaluate_promotion_route")
def evaluate_promotion_route(
    request: PromotionRequest,
    store: Annotated[EvalStore, Depends(_store)],
) -> Promotion:
    """Evaluate one promotion attempt and persist the decision."""
    try:
        return store.evaluate_and_store_promotion(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/promotions", response_model=list[Promotion], name="list_promotions")
def list_promotions_route(
    store: Annotated[EvalStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[Promotion]:
    """List promotion decisions for one org."""
    return store.list_promotions(org_id)
