"""Portfolio allocation and quota engine (CLIP-080).

Two deterministic controls from PRD §K (per-account publishing caps)
and §H (content feature allocation):

- **Quota engine** — `quota_policies` rows are org-scoped, idempotent
  per (org_id, quota_id) with slug ids. A policy caps publication
  volume for one scope: scope_kind ACCOUNT (count publication_jobs for
  one account_ref) or PLATFORM (count by platform slug), over a
  **rolling** window (DAILY = last 24h, WEEKLY = last 7 days, MONTHLY
  = last 30 days, UTC), as `max_count`. `check_quota` counts used
  publication_jobs inside the window and `evaluate_quota` is the pure
  gate: allowed iff used + proposed <= max (reason `within_quota` /
  `exceeds_quota`). Every check audits `quota_checks.evaluated`.
  Policies audited `quota_policies.registered`.
- **Portfolio allocation** — `allocate_portfolio(total_units, weights)`
  is the pure largest-remainder allocator: floor shares by weight,
  then the leftover units go by descending fractional part with
  deterministic key-order tie-breaks, so the result always sums to
  exactly `total_units`. Persisted `portfolio_allocations` rows
  (migration 0058, unique (org, allocation_id)) store the input
  weights and resulting split for audit; idempotent with conflict;
  audited `portfolio_allocations.recorded`.

Routes (CLIP-033 pattern): POST/GET /quota-policies, POST
/quota-policies/{quota_id}/check, POST /portfolio-allocations, GET
/portfolio-allocations?org_id=. No LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.publishing import PublicationJobRecord
from clipos_api.repository import Base


class QuotaScopeKind(str, Enum):
    """What a quota counts."""

    ACCOUNT = "ACCOUNT"
    PLATFORM = "PLATFORM"


class QuotaWindow(str, Enum):
    """Rolling window (UTC)."""

    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


WINDOW_DELTA = {
    QuotaWindow.DAILY: timedelta(days=1),
    QuotaWindow.WEEKLY: timedelta(days=7),
    QuotaWindow.MONTHLY: timedelta(days=30),
}


class QuotaPolicy(BaseModel):
    """One publication-volume cap."""

    model_config = ConfigDict(frozen=True)

    quota_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    scope_kind: QuotaScopeKind
    scope_ref: str = Field(min_length=1, max_length=255)
    window: QuotaWindow
    max_count: int = Field(ge=1, le=1_000_000)
    created_at: datetime | None = None


class RegisterQuotaRequest(BaseModel):
    """Request body for registering one quota policy."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    quota_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    scope_kind: QuotaScopeKind
    scope_ref: str = Field(min_length=1, max_length=255)
    window: QuotaWindow
    max_count: int = Field(ge=1, le=1_000_000)


class QuotaCheckRequest(BaseModel):
    """Request body for one quota check."""

    model_config = ConfigDict(frozen=True)

    proposed_count: int = Field(default=1, ge=1, le=1_000_000)


class QuotaEvaluation(BaseModel):
    """Pure quota gate output plus usage counts."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str = Field(min_length=1, max_length=32)
    used_count: int = Field(ge=0)
    proposed_count: int = Field(ge=1)
    max_count: int = Field(ge=1)


def evaluate_quota(used_count: int, max_count: int, proposed_count: int = 1) -> QuotaEvaluation:
    """Pure quota gate: allowed iff used + proposed <= max."""
    if used_count + proposed_count <= max_count:
        return QuotaEvaluation(
            allowed=True,
            reason="within_quota",
            used_count=used_count,
            proposed_count=proposed_count,
            max_count=max_count,
        )
    return QuotaEvaluation(
        allowed=False,
        reason="exceeds_quota",
        used_count=used_count,
        proposed_count=proposed_count,
        max_count=max_count,
    )


def allocate_portfolio(total_units: int, weights: dict[str, int]) -> dict[str, int]:
    """Pure largest-remainder integer allocation.

    Every weight must be >= 0 with at least one positive weight; the
    result sums to exactly total_units. Remainder units are assigned
    by descending fractional part, ties broken by key order — fully
    deterministic for identical inputs.
    """
    if total_units < 0:
        raise ValueError("total_units must be >= 0")
    if not weights:
        raise ValueError("weights must not be empty")
    if any(w < 0 for w in weights.values()):
        raise ValueError("weights must be >= 0")
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ValueError("weights must include a positive value")
    if total_units == 0:
        return {key: 0 for key in weights}
    keys = sorted(weights)
    base: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    assigned = 0
    for key in keys:
        exact = total_units * weights[key] / total_weight
        share = int(exact)
        base[key] = share
        assigned += share
        remainders.append((exact - share, key))
    leftover = total_units - assigned
    remainders.sort(key=lambda item: (-item[0], item[1]))
    for _, key in remainders[:leftover]:
        base[key] += 1
    return base


class QuotaPolicyRecord(Base):
    """SQL row for one quota policy."""

    __tablename__ = "quota_policies"
    __table_args__ = (
        sa.Index("uq_quota_policies_org_quota", "org_id", "quota_id", unique=True),
        sa.Index("ix_quota_policies_org_scope", "org_id", "scope_ref"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    quota_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    scope_kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    scope_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    window: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    max_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class PortfolioAllocationRecord(Base):
    """SQL row for one persisted allocation."""

    __tablename__ = "portfolio_allocations"
    __table_args__ = (
        sa.Index(
            "uq_portfolio_allocations_identity",
            "org_id",
            "allocation_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    allocation_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    total_units: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    weights: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    allocation: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class QuotaCheck(BaseModel):
    """One persisted quota check outcome."""

    model_config = ConfigDict(frozen=True)

    quota_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    allowed: bool
    reason: str
    used_count: int = Field(ge=0)
    proposed_count: int = Field(ge=1)
    max_count: int = Field(ge=1)
    checked_at: datetime | None = None


class PortfolioAllocation(BaseModel):
    """One persisted allocation run."""

    model_config = ConfigDict(frozen=True)

    allocation_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    total_units: int = Field(ge=0)
    weights: dict[str, int]
    allocation: dict[str, int]
    created_at: datetime | None = None


class RecordAllocationRequest(BaseModel):
    """Request body for one allocation run."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    allocation_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    total_units: int = Field(ge=0, le=1_000_000_000)
    weights: dict[str, int] = Field(min_length=1, max_length=64)


def _quota_from_row(row: QuotaPolicyRecord) -> QuotaPolicy:
    return QuotaPolicy(
        quota_id=row.quota_id,
        org_id=row.org_id,
        scope_kind=QuotaScopeKind(row.scope_kind),
        scope_ref=row.scope_ref,
        window=QuotaWindow(row.window),
        max_count=row.max_count,
        created_at=_aware(row.created_at),
    )


def _allocation_from_row(row: PortfolioAllocationRecord) -> PortfolioAllocation:
    return PortfolioAllocation(
        allocation_id=row.allocation_id,
        org_id=row.org_id,
        total_units=row.total_units,
        weights={str(k): int(v) for k, v in (row.weights or {}).items()},
        allocation={str(k): int(v) for k, v in (row.allocation or {}).items()},
        created_at=_aware(row.created_at),
    )


class QuotaStore:
    """Typed persistence for quota policies and portfolio allocations."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> QuotaStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_quota(self, request: RegisterQuotaRequest) -> QuotaPolicy:
        """Idempotent per (org, quota_id); changed fields conflict."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(QuotaPolicyRecord).where(
                    QuotaPolicyRecord.org_id == request.org_id,
                    QuotaPolicyRecord.quota_id == request.quota_id,
                )
            ).first()
            if existing is not None:
                stored = _quota_from_row(existing)
                if (
                    stored.scope_kind != request.scope_kind
                    or stored.scope_ref != request.scope_ref
                    or stored.window != request.window
                    or stored.max_count != request.max_count
                ):
                    raise ValueError(
                        f"quota policy already registered with different fields: {request.quota_id}"
                    )
                return stored
            row = QuotaPolicyRecord(
                quota_id=request.quota_id,
                org_id=request.org_id,
                scope_kind=request.scope_kind.value,
                scope_ref=request.scope_ref,
                window=request.window.value,
                max_count=request.max_count,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "quota_policies.registered",
                "quota_policies",
                f"quota_policy:{request.quota_id}",
                {
                    "org_id": request.org_id,
                    "scope_kind": request.scope_kind.value,
                    "scope_ref": request.scope_ref,
                    "window": request.window.value,
                    "max_count": request.max_count,
                },
            )
            return _quota_from_row(row)

    def list_quotas(self, org_id: str) -> list[QuotaPolicy]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(QuotaPolicyRecord)
                .where(QuotaPolicyRecord.org_id == org_id)
                .order_by(QuotaPolicyRecord.created_at, QuotaPolicyRecord.id)
            ).all()
            return [_quota_from_row(r) for r in rows]

    def check_quota(self, quota_id: str, org_id: str, proposed_count: int) -> QuotaCheck:
        """Count used publication_jobs in the rolling window and evaluate."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            policy = session.scalars(
                sa.select(QuotaPolicyRecord).where(
                    QuotaPolicyRecord.org_id == org_id,
                    QuotaPolicyRecord.quota_id == quota_id,
                )
            ).first()
            if policy is None:
                raise ValueError(f"quota policy not found: {quota_id}")
            window_start = now - WINDOW_DELTA[QuotaWindow(policy.window)]
            stmt = (
                sa.select(func.count())
                .select_from(PublicationJobRecord)
                .where(
                    PublicationJobRecord.org_id == org_id,
                    PublicationJobRecord.created_at >= window_start,
                )
            )
            if policy.scope_kind == QuotaScopeKind.ACCOUNT.value:
                stmt = stmt.where(PublicationJobRecord.account_ref == policy.scope_ref)
            else:
                stmt = stmt.where(PublicationJobRecord.platform == policy.scope_ref)
            used = int(session.scalar(stmt) or 0)
        evaluation = evaluate_quota(used, policy.max_count, proposed_count)
        audit.record(
            "quota_checks.evaluated",
            "quota_policies",
            f"quota_policy:{quota_id}",
            {
                "org_id": org_id,
                "allowed": evaluation.allowed,
                "reason": evaluation.reason,
                "used_count": used,
                "proposed_count": proposed_count,
            },
        )
        return QuotaCheck(
            quota_id=quota_id,
            org_id=org_id,
            allowed=evaluation.allowed,
            reason=evaluation.reason,
            used_count=evaluation.used_count,
            proposed_count=evaluation.proposed_count,
            max_count=evaluation.max_count,
            checked_at=now,
        )

    def record_allocation(self, request: RecordAllocationRequest) -> PortfolioAllocation:
        """Run the allocator and persist the result (idempotent)."""
        now = datetime.now(UTC)
        allocation = allocate_portfolio(request.total_units, request.weights)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(PortfolioAllocationRecord).where(
                    PortfolioAllocationRecord.org_id == request.org_id,
                    PortfolioAllocationRecord.allocation_id == request.allocation_id,
                )
            ).first()
            if existing is not None:
                stored = _allocation_from_row(existing)
                if stored.total_units != request.total_units or stored.weights != {
                    k: int(v) for k, v in request.weights.items()
                }:
                    raise ValueError(
                        f"allocation already recorded with different fields: "
                        f"{request.allocation_id}"
                    )
                return stored
            row = PortfolioAllocationRecord(
                allocation_id=request.allocation_id,
                org_id=request.org_id,
                total_units=request.total_units,
                weights={k: int(v) for k, v in request.weights.items()},
                allocation=allocation,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "portfolio_allocations.recorded",
                "portfolio_allocations",
                f"portfolio_allocation:{request.allocation_id}",
                {
                    "org_id": request.org_id,
                    "total_units": request.total_units,
                    "keys": sorted(allocation),
                },
            )
            return _allocation_from_row(row)

    def list_allocations(self, org_id: str) -> list[PortfolioAllocation]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(PortfolioAllocationRecord)
                .where(PortfolioAllocationRecord.org_id == org_id)
                .order_by(PortfolioAllocationRecord.created_at, PortfolioAllocationRecord.id)
            ).all()
            return [_allocation_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["portfolio-quota"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> QuotaStore:
    return QuotaStore.from_engine(engine)


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


@router.post("/quota-policies", response_model=QuotaPolicy, name="register_quota_policy")
def register_quota_policy_route(
    request: RegisterQuotaRequest,
    store: Annotated[QuotaStore, Depends(_store)],
) -> QuotaPolicy:
    """Register one quota policy (idempotent per org+quota_id)."""
    try:
        return store.register_quota(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/quota-policies",
    response_model=list[QuotaPolicy],
    name="list_quota_policies",
)
def list_quota_policies_route(
    store: Annotated[QuotaStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[QuotaPolicy]:
    """List quota policies for one org."""
    return store.list_quotas(org_id)


@router.post(
    "/quota-policies/{quota_id}/check",
    response_model=QuotaCheck,
    name="check_quota_policy",
)
def check_quota_policy_route(
    quota_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: QuotaCheckRequest,
    store: Annotated[QuotaStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> QuotaCheck:
    """Evaluate one quota policy against used publication volume."""
    try:
        return store.check_quota(quota_id, org_id, request.proposed_count)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/portfolio-allocations",
    response_model=PortfolioAllocation,
    name="record_portfolio_allocation",
)
def record_portfolio_allocation_route(
    request: RecordAllocationRequest,
    store: Annotated[QuotaStore, Depends(_store)],
) -> PortfolioAllocation:
    """Run the largest-remainder allocator and persist the result."""
    try:
        return store.record_allocation(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/portfolio-allocations",
    response_model=list[PortfolioAllocation],
    name="list_portfolio_allocations",
)
def list_portfolio_allocations_route(
    store: Annotated[QuotaStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[PortfolioAllocation]:
    """List persisted allocations for one org."""
    return store.list_allocations(org_id)
