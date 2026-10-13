"""Strategy-rule canary/promotion/rollback (CLIP-069).

Learned rules with a guarded rollout lifecycle (PRD §H "learned rules",
journey 5 "staging/canary -> promote or rollback"):

- **Rule records** — `strategy_rules` (migration 0047) are org-scoped,
  idempotent per (org_id, rule_id) with slug ids. `rule_kind` is a
  generic slug (never a vendor/model identifier); `parameters` is a
  JSON configuration blob owned by the rule's author.
- **Guarded lifecycle** — pure `rule_transition_allowed` table:
  DRAFT -> CANARY (canary), CANARY -> ACTIVE (promote), CANARY ->
  ROLLED_BACK (abort), ACTIVE -> ROLLED_BACK (rollback), ROLLED_BACK
  -> CANARY (re-canary after a fix). Everything else is rejected (409);
  same-state is a no-op. Promotion and rollback are therefore always
  explicit, audited moves — a rule never skips canary.
- **Audits** — `strategy_rules.registered` plus
  `strategy_rules.status_changed` carrying from/to, actor_ref, and
  reason_ref on every real move.

Routes (CLIP-033 pattern): POST /strategy-rules, GET
/strategy-rules/{rule_id}, GET /strategy-rules?org_id=&status=, POST
/strategy-rules/{rule_id}/transition. No LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base


class RuleStatus(str, Enum):
    """Strategy-rule rollout lifecycle."""

    DRAFT = "DRAFT"
    CANARY = "CANARY"
    ACTIVE = "ACTIVE"
    ROLLED_BACK = "ROLLED_BACK"


RULE_TRANSITIONS: dict[RuleStatus, frozenset[RuleStatus]] = {
    RuleStatus.DRAFT: frozenset({RuleStatus.CANARY}),
    RuleStatus.CANARY: frozenset({RuleStatus.ACTIVE, RuleStatus.ROLLED_BACK}),
    RuleStatus.ACTIVE: frozenset({RuleStatus.ROLLED_BACK}),
    RuleStatus.ROLLED_BACK: frozenset({RuleStatus.CANARY}),
}


def rule_transition_allowed(current: RuleStatus, target: RuleStatus) -> bool:
    """Pure: legal rule status move (same-state counts as allowed no-op)."""
    if current is target:
        return True
    return target in RULE_TRANSITIONS[current]


class StrategyRule(BaseModel):
    """One strategy rule with rollout status."""

    model_config = ConfigDict(frozen=True)

    rule_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    rule_kind: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: RuleStatus = RuleStatus.DRAFT
    created_at: datetime | None = None


class RegisterRuleRequest(BaseModel):
    """Request body for registering one strategy rule."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    rule_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    rule_kind: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    parameters: dict[str, Any] = Field(default_factory=dict)


class RuleTransitionRequest(BaseModel):
    """Request body for a rule rollout transition."""

    model_config = ConfigDict(frozen=True)

    status: RuleStatus
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)


class StrategyRuleRecord(Base):
    """SQL row for one strategy rule."""

    __tablename__ = "strategy_rules"
    __table_args__ = (
        sa.Index("uq_strategy_rules_org_rule", "org_id", "rule_id", unique=True),
        sa.Index("ix_strategy_rules_org_status", "org_id", "status"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    rule_kind: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _rule_from_row(row: StrategyRuleRecord) -> StrategyRule:
    return StrategyRule(
        rule_id=row.rule_id,
        org_id=row.org_id,
        title=row.title,
        rule_kind=row.rule_kind,
        parameters=dict(row.parameters or {}),
        status=RuleStatus(row.status),
        created_at=_aware(row.created_at),
    )


class StrategyRuleStore:
    """Typed persistence for strategy rules."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> StrategyRuleStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_rule(self, request: RegisterRuleRequest) -> StrategyRule:
        """Idempotent per (org, rule_id); changed fields conflict (409)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(StrategyRuleRecord).where(
                    StrategyRuleRecord.org_id == request.org_id,
                    StrategyRuleRecord.rule_id == request.rule_id,
                )
            ).first()
            if existing is not None:
                stored = _rule_from_row(existing)
                if (
                    stored.title != request.title
                    or stored.rule_kind != request.rule_kind
                    or stored.parameters != request.parameters
                ):
                    raise ValueError(
                        f"strategy rule already registered with different fields: {request.rule_id}"
                    )
                return stored
            row = StrategyRuleRecord(
                rule_id=request.rule_id,
                org_id=request.org_id,
                title=request.title,
                rule_kind=request.rule_kind,
                parameters=dict(request.parameters),
                status=RuleStatus.DRAFT.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "strategy_rules.registered",
                "strategy_rules",
                f"strategy_rule:{request.rule_id}",
                {
                    "org_id": request.org_id,
                    "rule_kind": request.rule_kind,
                },
            )
            return _rule_from_row(row)

    def get_rule(self, rule_id: str, org_id: str) -> StrategyRule | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(StrategyRuleRecord).where(
                    StrategyRuleRecord.rule_id == rule_id,
                    StrategyRuleRecord.org_id == org_id,
                )
            ).first()
            return _rule_from_row(row) if row is not None else None

    def list_rules(self, org_id: str, status: RuleStatus | None = None) -> list[StrategyRule]:
        with self._sessions() as session:
            stmt = sa.select(StrategyRuleRecord).where(StrategyRuleRecord.org_id == org_id)
            if status is not None:
                stmt = stmt.where(StrategyRuleRecord.status == status.value)
            rows = session.scalars(
                stmt.order_by(StrategyRuleRecord.created_at, StrategyRuleRecord.id)
            ).all()
            return [_rule_from_row(r) for r in rows]

    def transition_rule(
        self, rule_id: str, org_id: str, request: RuleTransitionRequest
    ) -> StrategyRule:
        """Apply a validated rollout transition (404 unknown, 409 illegal)."""
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(StrategyRuleRecord).where(
                    StrategyRuleRecord.rule_id == rule_id,
                    StrategyRuleRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"strategy rule not found: {rule_id}")
            current = RuleStatus(row.status)
            target = request.status
            if not rule_transition_allowed(current, target):
                raise ValueError(f"illegal rule transition: {current.value} -> {target.value}")
            if current is not target:
                row.status = target.value
                session.flush()
                audit.record(
                    "strategy_rules.status_changed",
                    "strategy_rules",
                    f"strategy_rule:{rule_id}",
                    {
                        "org_id": org_id,
                        "from_status": current.value,
                        "to_status": target.value,
                        "actor_ref": request.actor_ref,
                        "reason_ref": request.reason_ref,
                    },
                )
            return _rule_from_row(row)


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["strategy-rules"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> StrategyRuleStore:
    return StrategyRuleStore.from_engine(engine)


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


@router.post("/strategy-rules", response_model=StrategyRule, name="register_strategy_rule")
def register_strategy_rule_route(
    request: RegisterRuleRequest,
    store: Annotated[StrategyRuleStore, Depends(_store)],
) -> StrategyRule:
    """Register one strategy rule (idempotent per org+rule_id)."""
    try:
        return store.register_rule(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/strategy-rules/{rule_id}",
    response_model=StrategyRule,
    name="get_strategy_rule",
)
def get_strategy_rule_route(
    rule_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[StrategyRuleStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> StrategyRule:
    """Get one strategy rule by id and org."""
    stored = store.get_rule(rule_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"strategy rule not found: {rule_id}")
    return stored


@router.get(
    "/strategy-rules",
    response_model=list[StrategyRule],
    name="list_strategy_rules",
)
def list_strategy_rules_route(
    store: Annotated[StrategyRuleStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    status: Annotated[RuleStatus | None, Query()] = None,
) -> list[StrategyRule]:
    """List strategy rules for one org, optionally filtered by status."""
    return store.list_rules(org_id, status)


@router.post(
    "/strategy-rules/{rule_id}/transition",
    response_model=StrategyRule,
    name="transition_strategy_rule",
)
def transition_strategy_rule_route(
    rule_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: RuleTransitionRequest,
    store: Annotated[StrategyRuleStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> StrategyRule:
    """Apply a rollout transition (404 unknown, 409 illegal).

    canary = DRAFT->CANARY, promote = CANARY->ACTIVE, rollback =
    *->ROLLED_BACK, re-canary = ROLLED_BACK->CANARY.
    """
    try:
        return store.transition_rule(rule_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc
