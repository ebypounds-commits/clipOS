"""Budgets and spend authorization (CLIP-075).

PRD §I budgets — fail-closed authorization before provider spend:

- **Budgets** — `budgets` rows are org-scoped, idempotent per
  (org_id, budget_id) with slug ids. Each caps one `scope_key` (a
  generic slug — typically a cost_center value, or `org` for
  org-wide) over an explicit (period_start, period_end) window in one
  currency, as integer **amount_cents** (never floats). A period ends
  before it starts -> 422; changed fields on re-register -> 409.
- **Pure authorization** — `authorize_spend(cap_cents, spent_cents,
  requested_cents)` computes `remaining_before`, `allowed` (only when
  spent + requested <= cap), `remaining_after`, and a reason
  (`within_budget` / `exceeds_budget`). Currency mismatch between the
  budget and the request is a hard deny (`currency_mismatch`) — a
  budget in USD can never authorize EUR spend.
- **Spend basis** — already-recorded spend is the sum of CLIP-074
  `cost_entries` whose exact period window equals the budget window,
  currency matches, and whose source's `cost_center` equals the
  budget's `scope_key` (scope `org` counts every source). Deny is
  the default when anything is missing.
- **Persisted decisions** — every authorization (allowed or denied)
  is stored in `spend_authorizations` (migration 0053, unique
  (org, authorization_id)) with spent_before/allowed/reason so
  operators can audit the call; audited
  `spend_authorizations.evaluated`. Repeats are idempotent with
  conflict on changed inputs.

Routes (CLIP-033 pattern): POST/GET /budgets, GET
/budgets/{budget_id}, POST /budgets/{budget_id}/authorize, GET
/budgets/{budget_id}/authorizations. No LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.cost_ledger import CostEntryRecord, CostSourceRecord
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base
from clipos_api.revenue_ledger import CURRENCY_PATTERN


class Budget(BaseModel):
    """One spend cap for one scope over one window."""

    model_config = ConfigDict(frozen=True)

    budget_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    scope_key: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    amount_cents: int = Field(ge=0, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    period_start: date
    period_end: date
    created_at: datetime | None = None


class RegisterBudgetRequest(BaseModel):
    """Request body for registering one budget."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    budget_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    scope_key: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    amount_cents: int = Field(ge=0, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    period_start: date
    period_end: date


class SpendDecision(BaseModel):
    """Pure authorization output."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str = Field(min_length=1, max_length=64)
    cap_cents: int
    spent_cents: int
    requested_cents: int
    remaining_before: int
    remaining_after: int | None = Field(default=None)


class AuthorizeRequest(BaseModel):
    """Request body for one authorization evaluation."""

    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    amount_cents: int = Field(ge=0, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    actor_ref: str = Field(min_length=1, max_length=255)


class SpendAuthorization(BaseModel):
    """One persisted authorization decision."""

    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    budget_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    allowed: bool
    reason: str = Field(min_length=1, max_length=64)
    cap_cents: int
    spent_before_cents: int
    requested_cents: int
    remaining_before: int
    remaining_after: int | None = Field(default=None)
    actor_ref: str = Field(min_length=1, max_length=255)
    created_at: datetime | None = None


def authorize_spend(
    cap_cents: int,
    spent_cents: int,
    requested_cents: int,
) -> SpendDecision:
    """Pure fail-closed budget arithmetic.

    allowed only when spent + requested <= cap; remaining_after is
    None when denied (no promise about future headroom).
    """
    remaining_before = cap_cents - spent_cents
    if spent_cents + requested_cents <= cap_cents:
        return SpendDecision(
            allowed=True,
            reason="within_budget",
            cap_cents=cap_cents,
            spent_cents=spent_cents,
            requested_cents=requested_cents,
            remaining_before=remaining_before,
            remaining_after=remaining_before - requested_cents,
        )
    return SpendDecision(
        allowed=False,
        reason="exceeds_budget",
        cap_cents=cap_cents,
        spent_cents=spent_cents,
        requested_cents=requested_cents,
        remaining_before=remaining_before,
        remaining_after=None,
    )


class BudgetRecord(Base):
    """SQL row for one budget."""

    __tablename__ = "budgets"
    __table_args__ = (
        sa.Index("uq_budgets_org_budget", "org_id", "budget_id", unique=True),
        sa.Index("ix_budgets_org_scope", "org_id", "scope_key"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    budget_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    amount_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    period_start: Mapped[date] = mapped_column(sa.Date, nullable=False)
    period_end: Mapped[date] = mapped_column(sa.Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class SpendAuthorizationRecord(Base):
    """SQL row for one authorization decision."""

    __tablename__ = "spend_authorizations"
    __table_args__ = (
        sa.Index(
            "uq_spend_authorizations_identity",
            "org_id",
            "authorization_id",
            unique=True,
        ),
        sa.Index("ix_spend_authorizations_org_budget", "org_id", "budget_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    authorization_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    budget_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    allowed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    cap_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    spent_before_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    requested_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    remaining_before: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    remaining_after: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    actor_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _budget_from_row(row: BudgetRecord) -> Budget:
    return Budget(
        budget_id=row.budget_id,
        org_id=row.org_id,
        scope_key=row.scope_key,
        amount_cents=row.amount_cents,
        currency=row.currency,
        period_start=row.period_start,
        period_end=row.period_end,
        created_at=_aware(row.created_at),
    )


def _authorization_from_row(row: SpendAuthorizationRecord) -> SpendAuthorization:
    return SpendAuthorization(
        authorization_id=row.authorization_id,
        org_id=row.org_id,
        budget_id=row.budget_id,
        allowed=row.allowed,
        reason=row.reason,
        cap_cents=row.cap_cents,
        spent_before_cents=row.spent_before_cents,
        requested_cents=row.requested_cents,
        remaining_before=row.remaining_before,
        remaining_after=row.remaining_after,
        actor_ref=row.actor_ref,
        created_at=_aware(row.created_at),
    )


class BudgetStore:
    """Typed persistence for budgets and spend authorizations."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> BudgetStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_budget(self, request: RegisterBudgetRequest) -> Budget:
        """Idempotent per (org, budget_id); changed fields conflict."""
        now = datetime.now(UTC)
        if request.period_end < request.period_start:
            raise ValueError("period_end must not precede period_start")
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(BudgetRecord).where(
                    BudgetRecord.org_id == request.org_id,
                    BudgetRecord.budget_id == request.budget_id,
                )
            ).first()
            if existing is not None:
                stored = _budget_from_row(existing)
                if (
                    stored.scope_key != request.scope_key
                    or stored.amount_cents != request.amount_cents
                    or stored.currency != request.currency
                    or stored.period_start != request.period_start
                    or stored.period_end != request.period_end
                ):
                    raise ValueError(
                        f"budget already registered with different fields: {request.budget_id}"
                    )
                return stored
            row = BudgetRecord(
                budget_id=request.budget_id,
                org_id=request.org_id,
                scope_key=request.scope_key,
                amount_cents=request.amount_cents,
                currency=request.currency,
                period_start=request.period_start,
                period_end=request.period_end,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "budgets.registered",
                "budgets",
                f"budget:{request.budget_id}",
                {
                    "org_id": request.org_id,
                    "scope_key": request.scope_key,
                    "amount_cents": request.amount_cents,
                    "currency": request.currency,
                },
            )
            return _budget_from_row(row)

    def get_budget(self, budget_id: str, org_id: str) -> Budget | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(BudgetRecord).where(
                    BudgetRecord.budget_id == budget_id,
                    BudgetRecord.org_id == org_id,
                )
            ).first()
            return _budget_from_row(row) if row is not None else None

    def list_budgets(self, org_id: str) -> list[Budget]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(BudgetRecord)
                .where(BudgetRecord.org_id == org_id)
                .order_by(BudgetRecord.created_at, BudgetRecord.id)
            ).all()
            return [_budget_from_row(r) for r in rows]

    def _spent_cents(
        self,
        session: Session,
        org_id: str,
        scope_key: str,
        currency: str,
        period_start: date,
        period_end: date,
    ) -> int:
        """Sum cost_entries for the exact window+currency and scope.

        scope_key 'org' counts every source; otherwise the source's
        cost_center must match the scope.
        """
        stmt = (
            sa.select(sa.func.coalesce(sa.func.sum(CostEntryRecord.amount_cents), 0))
            .select_from(CostEntryRecord)
            .join(
                CostSourceRecord,
                sa.and_(
                    CostSourceRecord.org_id == CostEntryRecord.org_id,
                    CostSourceRecord.source_id == CostEntryRecord.source_id,
                ),
            )
            .where(
                CostEntryRecord.org_id == org_id,
                CostEntryRecord.currency == currency,
                CostEntryRecord.period_start == period_start,
                CostEntryRecord.period_end == period_end,
            )
        )
        if scope_key != "org":
            stmt = stmt.where(CostSourceRecord.cost_center == scope_key)
        return int(session.scalar(stmt) or 0)

    def authorize(
        self, budget_id: str, org_id: str, request: AuthorizeRequest
    ) -> SpendAuthorization:
        """Evaluate one spend request against the budget (fail-closed).

        Budget must exist (404); already-recorded spend is summed for
        the budget's exact window+currency+scope; currency mismatch
        denies; every outcome is persisted and audited.
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            budget = session.scalars(
                sa.select(BudgetRecord).where(
                    BudgetRecord.budget_id == budget_id,
                    BudgetRecord.org_id == org_id,
                )
            ).first()
            if budget is None:
                raise ValueError(f"budget not found: {budget_id}")
            existing = session.scalars(
                sa.select(SpendAuthorizationRecord).where(
                    SpendAuthorizationRecord.org_id == org_id,
                    SpendAuthorizationRecord.authorization_id == request.authorization_id,
                )
            ).first()
            if existing is not None:
                stored = _authorization_from_row(existing)
                if (
                    stored.budget_id != budget_id
                    or stored.requested_cents != request.amount_cents
                    or stored.actor_ref != request.actor_ref
                ):
                    raise ValueError(
                        f"authorization already recorded with different fields: "
                        f"{request.authorization_id}"
                    )
                return stored
            spent = self._spent_cents(
                session,
                org_id,
                budget.scope_key,
                budget.currency,
                budget.period_start,
                budget.period_end,
            )
            if request.currency != budget.currency:
                decision = SpendDecision(
                    allowed=False,
                    reason="currency_mismatch",
                    cap_cents=budget.amount_cents,
                    spent_cents=spent,
                    requested_cents=request.amount_cents,
                    remaining_before=budget.amount_cents - spent,
                    remaining_after=None,
                )
            else:
                decision = authorize_spend(budget.amount_cents, spent, request.amount_cents)
            row = SpendAuthorizationRecord(
                authorization_id=request.authorization_id,
                org_id=org_id,
                budget_id=budget_id,
                allowed=decision.allowed,
                reason=decision.reason,
                cap_cents=decision.cap_cents,
                spent_before_cents=decision.spent_cents,
                requested_cents=decision.requested_cents,
                remaining_before=decision.remaining_before,
                remaining_after=decision.remaining_after,
                actor_ref=request.actor_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "spend_authorizations.evaluated",
                "spend_authorizations",
                f"spend_authorization:{request.authorization_id}",
                {
                    "org_id": org_id,
                    "budget_id": budget_id,
                    "allowed": decision.allowed,
                    "reason": decision.reason,
                    "requested_cents": decision.requested_cents,
                },
            )
            return _authorization_from_row(row)

    def list_authorizations(self, org_id: str, budget_id: str) -> list[SpendAuthorization]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(SpendAuthorizationRecord)
                .where(
                    SpendAuthorizationRecord.org_id == org_id,
                    SpendAuthorizationRecord.budget_id == budget_id,
                )
                .order_by(SpendAuthorizationRecord.created_at, SpendAuthorizationRecord.id)
            ).all()
            return [_authorization_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["budgets"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> BudgetStore:
    return BudgetStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post("/budgets", response_model=Budget, name="register_budget")
def register_budget_route(
    request: RegisterBudgetRequest,
    store: Annotated[BudgetStore, Depends(_store)],
) -> Budget:
    """Register one budget (idempotent per org+budget_id)."""
    try:
        return store.register_budget(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/budgets/{budget_id}", response_model=Budget, name="get_budget")
def get_budget_route(
    budget_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[BudgetStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Budget:
    """Get one budget by id and org."""
    stored = store.get_budget(budget_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"budget not found: {budget_id}")
    return stored


@router.get("/budgets", response_model=list[Budget], name="list_budgets")
def list_budgets_route(
    store: Annotated[BudgetStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[Budget]:
    """List budgets for one org."""
    return store.list_budgets(org_id)


@router.post(
    "/budgets/{budget_id}/authorize",
    response_model=SpendAuthorization,
    name="authorize_spend_route",
)
def authorize_spend_route(
    budget_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: AuthorizeRequest,
    store: Annotated[BudgetStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> SpendAuthorization:
    """Evaluate one spend request (404 unknown, 409 changed repeat).

    Allowed and denied outcomes are both persisted for audit.
    """
    try:
        return store.authorize(budget_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/budgets/{budget_id}/authorizations",
    response_model=list[SpendAuthorization],
    name="list_spend_authorizations",
)
def list_spend_authorizations_route(
    budget_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[BudgetStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[SpendAuthorization]:
    """List persisted authorization decisions for one budget."""
    return store.list_authorizations(org_id, budget_id)
