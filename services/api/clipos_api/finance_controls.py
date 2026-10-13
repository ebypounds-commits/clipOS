"""Anomaly detection and finance approval gates (CLIP-077).

Closes M18 with two fail-closed finance controls (PRD §I / §K):

- **Anomaly detection** — `detect_cost_anomaly(candidate_cents,
  baseline_cents, z_threshold)` is pure: z = (candidate - mean) /
  population-stddev of the baseline (zero stddev -> z is 0 when the
  candidate equals the mean, infinite otherwise); an anomaly is a
  candidate above mean + z_threshold * stddev (only over-cost is
  anomalous). Fewer than two baseline points -> `insufficient_data`
  (never flags). `run_anomaly_check` scores a CLIP-074 ledger entry
  against the other entries of the same source + category and persists
  a `cost_anomalies` row (migration 0055, unique (org, anomaly_id))
  with the z score and verdict; audited `cost_anomalies.recorded`.
- **Finance approval gates** — `finance_approvals` (same migration)
  track a requested amount through PENDING -> APPROVED / REJECTED
  (one-shot; same-state no-op; illegal moves 409) with actor/reason
  refs on every move; audited `finance_approvals.submitted` and
  `finance_approvals.resolved`. `evaluate_approval_gate(status,
  approved_cents, requested_cents, currency...)` is the pure gate:
  only an APPROVED record whose approved amount >= the request and
  whose currency matches can allow; everything else denies with an
  explicit reason (`not_approved`, `amount_exceeds_approved`,
  `currency_mismatch`). Gate checks audit `finance_approvals.checked`.

Routes (CLIP-033 pattern): POST /cost-anomalies/check, GET
/cost-anomalies?org_id=, POST/GET /finance-approvals, POST
/finance-approvals/{approval_id}/resolve, POST
/finance-approvals/{approval_id}/gate. No LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from math import sqrt
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.cost_ledger import CostEntryRecord
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base
from clipos_api.revenue_ledger import CURRENCY_PATTERN

DEFAULT_Z_THRESHOLD = 3.0


class AnomalyVerdict(str, Enum):
    """Outcome of one anomaly check."""

    ANOMALY = "ANOMALY"
    NORMAL = "NORMAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class AnomalyCheck(BaseModel):
    """Pure z-score anomaly verdict."""

    model_config = ConfigDict(frozen=True)

    verdict: AnomalyVerdict
    z_score: float | None = Field(default=None)
    mean_cents: float | None = None
    stddev_cents: float | None = None
    threshold: float = Field(gt=0.0)
    detail: str = Field(min_length=1, max_length=64)


def detect_cost_anomaly(
    candidate_cents: int,
    baseline_cents: list[int],
    z_threshold: float = DEFAULT_Z_THRESHOLD,
) -> AnomalyCheck:
    """Pure over-cost detector (population stddev).

    Fewer than two baseline points -> INSUFFICIENT_DATA. Zero
    stddev: equal candidate is NORMAL (z=0), a higher candidate is
    an infinite-z anomaly, a lower one NORMAL (under-cost is not an
    anomaly).
    """
    if z_threshold <= 0:
        raise ValueError("z_threshold must be positive")
    if len(baseline_cents) < 2:
        return AnomalyCheck(
            verdict=AnomalyVerdict.INSUFFICIENT_DATA,
            z_score=None,
            mean_cents=None,
            stddev_cents=None,
            threshold=z_threshold,
            detail="baseline_below_minimum",
        )
    mean = sum(baseline_cents) / len(baseline_cents)
    variance = sum((v - mean) ** 2 for v in baseline_cents) / len(baseline_cents)
    stddev = sqrt(variance)
    if stddev == 0.0:
        if candidate_cents > mean:
            z_score = float("inf")
            verdict = AnomalyVerdict.ANOMALY
            detail = "zero_variance_over_mean"
        else:
            z_score = 0.0
            verdict = AnomalyVerdict.NORMAL
            detail = "zero_variance_at_or_below_mean"
        return AnomalyCheck(
            verdict=verdict,
            z_score=z_score,
            mean_cents=mean,
            stddev_cents=0.0,
            threshold=z_threshold,
            detail=detail,
        )
    z_score = (candidate_cents - mean) / stddev
    if z_score > z_threshold:
        verdict = AnomalyVerdict.ANOMALY
        detail = "z_above_threshold"
    else:
        verdict = AnomalyVerdict.NORMAL
        detail = "z_within_threshold"
    return AnomalyCheck(
        verdict=verdict,
        z_score=z_score,
        mean_cents=mean,
        stddev_cents=stddev,
        threshold=z_threshold,
        detail=detail,
    )


class ApprovalStatus(str, Enum):
    """One-shot finance approval lifecycle."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


APPROVAL_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.PENDING: frozenset({ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}),
    ApprovalStatus.APPROVED: frozenset(),
    ApprovalStatus.REJECTED: frozenset(),
}


class GateDecision(BaseModel):
    """Pure finance-approval gate output."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str = Field(min_length=1, max_length=64)
    approval_status: str = Field(min_length=1, max_length=16)
    approved_cents: int
    requested_cents: int


def evaluate_approval_gate(
    status: ApprovalStatus,
    approved_cents: int,
    approved_currency: str,
    requested_cents: int,
    requested_currency: str,
) -> GateDecision:
    """Pure gate: only APPROVED + currency match + amount within cap."""
    if status is not ApprovalStatus.APPROVED:
        return GateDecision(
            allowed=False,
            reason="not_approved",
            approval_status=status.value,
            approved_cents=approved_cents,
            requested_cents=requested_cents,
        )
    if requested_currency != approved_currency:
        return GateDecision(
            allowed=False,
            reason="currency_mismatch",
            approval_status=status.value,
            approved_cents=approved_cents,
            requested_cents=requested_cents,
        )
    if requested_cents > approved_cents:
        return GateDecision(
            allowed=False,
            reason="amount_exceeds_approved",
            approval_status=status.value,
            approved_cents=approved_cents,
            requested_cents=requested_cents,
        )
    return GateDecision(
        allowed=True,
        reason="approved",
        approval_status=status.value,
        approved_cents=approved_cents,
        requested_cents=requested_cents,
    )


class CostAnomalyRecord(Base):
    """SQL row for one anomaly check."""

    __tablename__ = "cost_anomalies"
    __table_args__ = (
        sa.Index("uq_cost_anomalies_identity", "org_id", "anomaly_id", unique=True),
        sa.Index("ix_cost_anomalies_org_entry", "org_id", "entry_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    anomaly_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entry_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    verdict: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    z_score: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    mean_cents: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    stddev_cents: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    threshold: Mapped[float] = mapped_column(sa.Float, nullable=False)
    detail: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class FinanceApprovalRecord(Base):
    """SQL row for one finance approval."""

    __tablename__ = "finance_approvals"
    __table_args__ = (
        sa.Index(
            "uq_finance_approvals_identity",
            "org_id",
            "approval_id",
            unique=True,
        ),
        sa.Index("ix_finance_approvals_org_status", "org_id", "status"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    approval_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    purpose_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    amount_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    submitted_by: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    reason_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class CostAnomaly(BaseModel):
    """One persisted anomaly check."""

    model_config = ConfigDict(frozen=True)

    anomaly_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    entry_id: str = Field(min_length=1, max_length=128)
    verdict: AnomalyVerdict
    z_score: float | None = None
    mean_cents: float | None = None
    stddev_cents: float | None = None
    threshold: float
    detail: str
    created_at: datetime | None = None


class FinanceApproval(BaseModel):
    """One finance approval request."""

    model_config = ConfigDict(frozen=True)

    approval_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    purpose_ref: str = Field(min_length=1, max_length=512)
    amount_cents: int = Field(ge=0, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    status: ApprovalStatus
    submitted_by: str = Field(min_length=1, max_length=255)
    resolved_by: str | None = Field(default=None, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = None
    resolved_at: datetime | None = None


def _anomaly_from_row(row: CostAnomalyRecord) -> CostAnomaly:
    return CostAnomaly(
        anomaly_id=row.anomaly_id,
        org_id=row.org_id,
        entry_id=row.entry_id,
        verdict=AnomalyVerdict(row.verdict),
        z_score=row.z_score,
        mean_cents=row.mean_cents,
        stddev_cents=row.stddev_cents,
        threshold=row.threshold,
        detail=row.detail,
        created_at=_aware(row.created_at),
    )


def _approval_from_row(row: FinanceApprovalRecord) -> FinanceApproval:
    return FinanceApproval(
        approval_id=row.approval_id,
        org_id=row.org_id,
        purpose_ref=row.purpose_ref,
        amount_cents=row.amount_cents,
        currency=row.currency,
        status=ApprovalStatus(row.status),
        submitted_by=row.submitted_by,
        resolved_by=row.resolved_by,
        reason_ref=row.reason_ref,
        created_at=_aware(row.created_at),
        resolved_at=_aware(row.resolved_at),
    )


class CheckAnomalyRequest(BaseModel):
    """Request body for one anomaly check."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    anomaly_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    entry_id: str = Field(min_length=1, max_length=128)
    z_threshold: float = Field(default=DEFAULT_Z_THRESHOLD, gt=0.0, le=100.0)


class SubmitApprovalRequest(BaseModel):
    """Request body for submitting one finance approval."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    approval_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    purpose_ref: str = Field(min_length=1, max_length=512)
    amount_cents: int = Field(ge=0, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    submitted_by: str = Field(min_length=1, max_length=255)


class ResolveApprovalRequest(BaseModel):
    """Request body for resolving one approval."""

    model_config = ConfigDict(frozen=True)

    status: ApprovalStatus
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)


class GateRequest(BaseModel):
    """Request body for one gate check."""

    model_config = ConfigDict(frozen=True)

    amount_cents: int = Field(ge=0, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)


class FinanceControlStore:
    """Typed persistence for anomaly checks and finance approvals."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> FinanceControlStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def run_anomaly_check(self, request: CheckAnomalyRequest) -> CostAnomaly:
        """Score one ledger entry against its source+category peers.

        Entry must exist (404); idempotent per (org, anomaly_id).
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(CostAnomalyRecord).where(
                    CostAnomalyRecord.org_id == request.org_id,
                    CostAnomalyRecord.anomaly_id == request.anomaly_id,
                )
            ).first()
            if existing is not None:
                stored = _anomaly_from_row(existing)
                if stored.entry_id != request.entry_id or stored.threshold != request.z_threshold:
                    raise ValueError(
                        f"anomaly check already recorded with different fields: "
                        f"{request.anomaly_id}"
                    )
                return stored
            entry = session.scalars(
                sa.select(CostEntryRecord).where(
                    CostEntryRecord.org_id == request.org_id,
                    CostEntryRecord.entry_id == request.entry_id,
                )
            ).first()
            if entry is None:
                raise ValueError(f"cost entry not found: {request.entry_id}")
            peers = session.scalars(
                sa.select(CostEntryRecord).where(
                    CostEntryRecord.org_id == request.org_id,
                    CostEntryRecord.source_id == entry.source_id,
                    CostEntryRecord.cost_category == entry.cost_category,
                    CostEntryRecord.id != entry.id,
                )
            ).all()
        baseline = [p.amount_cents for p in peers]
        check = detect_cost_anomaly(entry.amount_cents, baseline, request.z_threshold)
        row = CostAnomalyRecord(
            anomaly_id=request.anomaly_id,
            org_id=request.org_id,
            entry_id=request.entry_id,
            verdict=check.verdict.value,
            z_score=check.z_score,
            mean_cents=check.mean_cents,
            stddev_cents=check.stddev_cents,
            threshold=check.threshold,
            detail=check.detail,
            created_at=now,
        )
        with self._sessions.begin() as session:
            raced = session.scalars(
                sa.select(CostAnomalyRecord).where(
                    CostAnomalyRecord.org_id == request.org_id,
                    CostAnomalyRecord.anomaly_id == request.anomaly_id,
                )
            ).first()
            if raced is not None:
                return _anomaly_from_row(raced)
            session.add(row)
            session.flush()
            audit.record(
                "cost_anomalies.recorded",
                "cost_anomalies",
                f"cost_anomaly:{request.anomaly_id}",
                {
                    "org_id": request.org_id,
                    "entry_id": request.entry_id,
                    "verdict": check.verdict.value,
                    "detail": check.detail,
                },
            )
            return _anomaly_from_row(row)

    def list_anomalies(self, org_id: str) -> list[CostAnomaly]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(CostAnomalyRecord)
                .where(CostAnomalyRecord.org_id == org_id)
                .order_by(CostAnomalyRecord.created_at, CostAnomalyRecord.id)
            ).all()
            return [_anomaly_from_row(r) for r in rows]

    def submit_approval(self, request: SubmitApprovalRequest) -> FinanceApproval:
        """Create one PENDING approval (idempotent; conflict 409)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(FinanceApprovalRecord).where(
                    FinanceApprovalRecord.org_id == request.org_id,
                    FinanceApprovalRecord.approval_id == request.approval_id,
                )
            ).first()
            if existing is not None:
                stored = _approval_from_row(existing)
                if (
                    stored.purpose_ref != request.purpose_ref
                    or stored.amount_cents != request.amount_cents
                    or stored.currency != request.currency
                    or stored.submitted_by != request.submitted_by
                ):
                    raise ValueError(
                        f"finance approval already submitted with different fields: "
                        f"{request.approval_id}"
                    )
                return stored
            row = FinanceApprovalRecord(
                approval_id=request.approval_id,
                org_id=request.org_id,
                purpose_ref=request.purpose_ref,
                amount_cents=request.amount_cents,
                currency=request.currency,
                status=ApprovalStatus.PENDING.value,
                submitted_by=request.submitted_by,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "finance_approvals.submitted",
                "finance_approvals",
                f"finance_approval:{request.approval_id}",
                {
                    "org_id": request.org_id,
                    "amount_cents": request.amount_cents,
                    "currency": request.currency,
                },
            )
            return _approval_from_row(row)

    def get_approval(self, approval_id: str, org_id: str) -> FinanceApproval | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(FinanceApprovalRecord).where(
                    FinanceApprovalRecord.approval_id == approval_id,
                    FinanceApprovalRecord.org_id == org_id,
                )
            ).first()
            return _approval_from_row(row) if row is not None else None

    def list_approvals(self, org_id: str) -> list[FinanceApproval]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(FinanceApprovalRecord)
                .where(FinanceApprovalRecord.org_id == org_id)
                .order_by(FinanceApprovalRecord.created_at, FinanceApprovalRecord.id)
            ).all()
            return [_approval_from_row(r) for r in rows]

    def resolve_approval(
        self, approval_id: str, org_id: str, request: ResolveApprovalRequest
    ) -> FinanceApproval:
        """One-shot PENDING -> APPROVED/REJECTED (404/409)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(FinanceApprovalRecord).where(
                    FinanceApprovalRecord.approval_id == approval_id,
                    FinanceApprovalRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"finance approval not found: {approval_id}")
            current = ApprovalStatus(row.status)
            target = request.status
            if current is ApprovalStatus.PENDING and target in (
                ApprovalStatus.APPROVED,
                ApprovalStatus.REJECTED,
            ):
                row.status = target.value
                row.resolved_by = request.actor_ref
                row.reason_ref = request.reason_ref
                row.resolved_at = now
                session.flush()
                audit.record(
                    "finance_approvals.resolved",
                    "finance_approvals",
                    f"finance_approval:{approval_id}",
                    {
                        "org_id": org_id,
                        "to_status": target.value,
                        "actor_ref": request.actor_ref,
                        "reason_ref": request.reason_ref,
                    },
                )
                return _approval_from_row(row)
            if current is target:
                return _approval_from_row(row)
            raise ValueError(f"illegal approval transition: {current.value} -> {target.value}")

    def gate(self, approval_id: str, org_id: str, request: GateRequest) -> GateDecision:
        """Evaluate a spend request against one approval (fail-closed)."""
        approval = self.get_approval(approval_id, org_id)
        if approval is None:
            raise ValueError(f"finance approval not found: {approval_id}")
        decision = evaluate_approval_gate(
            approval.status,
            approval.amount_cents,
            approval.currency,
            request.amount_cents,
            request.currency,
        )
        audit.record(
            "finance_approvals.checked",
            "finance_approvals",
            f"finance_approval:{approval_id}",
            {
                "org_id": org_id,
                "allowed": decision.allowed,
                "reason": decision.reason,
                "requested_cents": request.amount_cents,
            },
        )
        return decision


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["finance-controls"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> FinanceControlStore:
    return FinanceControlStore.from_engine(engine)


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


@router.post(
    "/cost-anomalies/check",
    response_model=CostAnomaly,
    name="check_cost_anomaly",
)
def check_cost_anomaly_route(
    request: CheckAnomalyRequest,
    store: Annotated[FinanceControlStore, Depends(_store)],
) -> CostAnomaly:
    """Score one cost entry against its source+category peers."""
    try:
        return store.run_anomaly_check(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/cost-anomalies",
    response_model=list[CostAnomaly],
    name="list_cost_anomalies",
)
def list_cost_anomalies_route(
    store: Annotated[FinanceControlStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[CostAnomaly]:
    """List persisted anomaly checks for one org."""
    return store.list_anomalies(org_id)


@router.post(
    "/finance-approvals",
    response_model=FinanceApproval,
    name="submit_finance_approval",
)
def submit_finance_approval_route(
    request: SubmitApprovalRequest,
    store: Annotated[FinanceControlStore, Depends(_store)],
) -> FinanceApproval:
    """Submit one PENDING finance approval (idempotent)."""
    try:
        return store.submit_approval(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/finance-approvals/{approval_id}",
    response_model=FinanceApproval,
    name="get_finance_approval",
)
def get_finance_approval_route(
    approval_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[FinanceControlStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> FinanceApproval:
    """Get one finance approval by id and org."""
    stored = store.get_approval(approval_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"finance approval not found: {approval_id}")
    return stored


@router.get(
    "/finance-approvals",
    response_model=list[FinanceApproval],
    name="list_finance_approvals",
)
def list_finance_approvals_route(
    store: Annotated[FinanceControlStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[FinanceApproval]:
    """List finance approvals for one org."""
    return store.list_approvals(org_id)


@router.post(
    "/finance-approvals/{approval_id}/resolve",
    response_model=FinanceApproval,
    name="resolve_finance_approval",
)
def resolve_finance_approval_route(
    approval_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: ResolveApprovalRequest,
    store: Annotated[FinanceControlStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> FinanceApproval:
    """Approve or reject one PENDING approval (404 unknown, 409 illegal)."""
    try:
        return store.resolve_approval(approval_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/finance-approvals/{approval_id}/gate",
    response_model=GateDecision,
    name="gate_finance_approval",
)
def gate_finance_approval_route(
    approval_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: GateRequest,
    store: Annotated[FinanceControlStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> GateDecision:
    """Evaluate a spend request against one approval (fail-closed)."""
    try:
        return store.gate(approval_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc
