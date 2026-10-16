"""Owner-gated public publishing (CLIP-099).

`07_SECURITY_GOVERNANCE_RIGHTS.md`: "New platform account starts
`PRIVATE_TEST`. Public mode requires explicit owner approval after
successful private test." `00_README_FIRST.md`: "owner gate before first
public posting per real platform account." This module implements that gate.

The gate is an **owner approval record**, not a configuration flag:

- `owner_approvals` rows are org-scoped, idempotent per (org, approval_id),
  and bind one `(org, platform, account_ref)` to an explicit owner decision.
  An approval records the evidence of the successful private test it
  follows (`private_test_ref`), the owner identity reference, and an
  optional expiry. Audited `owner_approvals.granted`.
- Approvals can be **revoked** (append-only status change, audited
  `owner_approvals.revoked`) so an owner can close public publishing for one
  account without touching anyone else's.

Pure decision logic, no I/O:

- :func:`evaluate_public_gate` returns ALLOWED / DENIED plus a machine
  reason. Public publishing requires **all** of: a granted (not revoked, not
  expired) owner approval for that exact account, and a recorded successful
  private test for that account. Every missing precondition has its own
  reason code so an operator can see precisely what to do next.
- :func:`gate_summary` folds the approvals and private-test history for one
  org into a per-account readiness view, so an operator can answer "which
  accounts may publish publicly right now, and which are blocked and why".

The default is closed: an account with no approval is DENIED, and a
non-public visibility never consults the gate at all.

Routes (CLIP-033 pattern): POST/GET /publishing/owner-approvals,
POST /publishing/owner-approvals/{approval_id}/revoke, POST
/publishing/private-tests, GET /publishing/gate-summary. No LLM, no network.
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
from clipos_api.repository import Base


class GateDecision(str, Enum):
    """Outcome of the owner gate for one account."""

    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


class GateReason(str, Enum):
    """Why the gate reached its decision (machine-readable)."""

    ALLOWED = "owner_approval_active"
    NO_APPROVAL = "no_owner_approval"
    APPROVAL_REVOKED = "owner_approval_revoked"
    APPROVAL_EXPIRED = "owner_approval_expired"
    NO_PRIVATE_TEST = "no_successful_private_test"


class ApprovalStatus(str, Enum):
    """Lifecycle of one owner approval."""

    GRANTED = "GRANTED"
    REVOKED = "REVOKED"


class PrivateTestOutcome(str, Enum):
    """Outcome of a private test for one account."""

    PASSED = "PASSED"
    FAILED = "FAILED"


class GateResult(BaseModel):
    """Pure gate verdict."""

    model_config = ConfigDict(frozen=True)

    decision: GateDecision
    reason: GateReason
    account_ref: str
    approval_id: str | None = None


class OwnerApproval(BaseModel):
    """One owner decision permitting public publishing for an account."""

    model_config = ConfigDict(from_attributes=True)

    approval_id: str
    org_id: str
    platform: str
    account_ref: str
    status: ApprovalStatus
    owner_ref: str
    private_test_ref: str
    note: str
    granted_at: datetime
    revoked_at: datetime | None = None
    expires_at: datetime | None = None


class PrivateTest(BaseModel):
    """One recorded private test for an account."""

    model_config = ConfigDict(from_attributes=True)

    test_ref: str
    org_id: str
    platform: str
    account_ref: str
    outcome: PrivateTestOutcome
    detail: str
    recorded_at: datetime


class AccountGateReadiness(BaseModel):
    """Per-account readiness view."""

    model_config = ConfigDict(frozen=True)

    platform: str
    account_ref: str
    decision: GateDecision
    reason: GateReason
    approval_id: str | None = None


class GateSummary(BaseModel):
    """Every account the org has touched, with its gate state."""

    model_config = ConfigDict(frozen=True)

    org_id: str
    accounts: list[AccountGateReadiness] = Field(default_factory=list)
    allowed_count: int
    denied_count: int


class GrantApprovalRequest(BaseModel):
    """Grant one owner approval for an account."""

    approval_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    account_ref: str = Field(min_length=1, max_length=255)
    owner_ref: str = Field(min_length=1, max_length=255)
    private_test_ref: str = Field(min_length=1, max_length=255)
    note: str = Field(default="", max_length=1000)
    expires_at: datetime | None = None


class RevokeApprovalRequest(BaseModel):
    """Revoke one owner approval."""

    org_id: str = Field(min_length=1, max_length=64)
    owner_ref: str = Field(min_length=1, max_length=255)
    note: str = Field(default="", max_length=1000)


class RecordPrivateTestRequest(BaseModel):
    """Record the outcome of one private test for an account."""

    test_ref: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    account_ref: str = Field(min_length=1, max_length=255)
    outcome: PrivateTestOutcome
    detail: str = Field(default="", max_length=1000)


class OwnerApprovalRecord(Base):
    """SQL row for one owner approval."""

    __tablename__ = "owner_approvals"
    __table_args__ = (
        sa.Index("uq_owner_approvals_org_approval", "org_id", "approval_id", unique=True),
        sa.Index(
            "ix_owner_approvals_org_account",
            "org_id",
            "platform",
            "account_ref",
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    approval_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    account_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    owner_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    private_test_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    note: Mapped[str] = mapped_column(sa.String(1000), nullable=False, default="")
    granted_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class PrivateTestRecord(Base):
    """SQL row for one private test."""

    __tablename__ = "private_tests"
    __table_args__ = (
        sa.Index("uq_private_tests_org_test", "org_id", "test_ref", unique=True),
        sa.Index("ix_private_tests_org_account", "org_id", "platform", "account_ref"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    test_ref: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    account_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    detail: Mapped[str] = mapped_column(sa.String(1000), nullable=False, default="")
    recorded_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def evaluate_public_gate(
    approvals: list[OwnerApproval],
    private_tests: list[PrivateTest],
    account_ref: str,
    now: datetime,
) -> GateResult:
    """Pure: may this account publish publicly right now?

    Fail-closed and ordered, so the reason names the *next* thing an
    operator must do. An approval that is revoked or expired is treated as
    absent but reported distinctly from never having had one.
    """
    relevant = [a for a in approvals if a.account_ref == account_ref]
    if not relevant:
        return GateResult(
            decision=GateDecision.DENIED,
            reason=GateReason.NO_APPROVAL,
            account_ref=account_ref,
        )

    latest = max(relevant, key=lambda a: a.granted_at)
    if latest.status is ApprovalStatus.REVOKED:
        return GateResult(
            decision=GateDecision.DENIED,
            reason=GateReason.APPROVAL_REVOKED,
            account_ref=account_ref,
            approval_id=latest.approval_id,
        )
    if latest.expires_at is not None and _aware(latest.expires_at) <= now:
        return GateResult(
            decision=GateDecision.DENIED,
            reason=GateReason.APPROVAL_EXPIRED,
            account_ref=account_ref,
            approval_id=latest.approval_id,
        )

    passed = [
        t
        for t in private_tests
        if t.account_ref == account_ref and t.outcome is PrivateTestOutcome.PASSED
    ]
    if not passed:
        return GateResult(
            decision=GateDecision.DENIED,
            reason=GateReason.NO_PRIVATE_TEST,
            account_ref=account_ref,
            approval_id=latest.approval_id,
        )

    return GateResult(
        decision=GateDecision.ALLOWED,
        reason=GateReason.ALLOWED,
        account_ref=account_ref,
        approval_id=latest.approval_id,
    )


def gate_summary(
    org_id: str,
    approvals: list[OwnerApproval],
    private_tests: list[PrivateTest],
    now: datetime,
) -> GateSummary:
    """Pure: every known account and its current gate state."""
    keys: set[tuple[str, str]] = set()
    for approval in approvals:
        keys.add((approval.platform, approval.account_ref))
    for test in private_tests:
        keys.add((test.platform, test.account_ref))

    accounts: list[AccountGateReadiness] = []
    for platform, account_ref in sorted(keys):
        result = evaluate_public_gate(approvals, private_tests, account_ref, now)
        accounts.append(
            AccountGateReadiness(
                platform=platform,
                account_ref=account_ref,
                decision=result.decision,
                reason=result.reason,
                approval_id=result.approval_id,
            )
        )
    allowed = sum(1 for a in accounts if a.decision is GateDecision.ALLOWED)
    return GateSummary(
        org_id=org_id,
        accounts=accounts,
        allowed_count=allowed,
        denied_count=len(accounts) - allowed,
    )


def _approval_from_row(row: OwnerApprovalRecord) -> OwnerApproval:
    return OwnerApproval(
        approval_id=row.approval_id,
        org_id=row.org_id,
        platform=row.platform,
        account_ref=row.account_ref,
        status=ApprovalStatus(row.status),
        owner_ref=row.owner_ref,
        private_test_ref=row.private_test_ref,
        note=row.note,
        granted_at=_aware(row.granted_at),
        revoked_at=_aware(row.revoked_at) if row.revoked_at else None,
        expires_at=_aware(row.expires_at) if row.expires_at else None,
    )


def _test_from_row(row: PrivateTestRecord) -> PrivateTest:
    return PrivateTest(
        test_ref=row.test_ref,
        org_id=row.org_id,
        platform=row.platform,
        account_ref=row.account_ref,
        outcome=PrivateTestOutcome(row.outcome),
        detail=row.detail,
        recorded_at=_aware(row.recorded_at),
    )


class PublicGateStore:
    """Typed persistence for owner approvals and private tests."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> PublicGateStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # -- approvals ---------------------------------------------------------

    def grant_approval(self, request: GrantApprovalRequest) -> OwnerApproval:
        """Idempotent per (org, approval_id)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(OwnerApprovalRecord).where(
                    OwnerApprovalRecord.org_id == request.org_id,
                    OwnerApprovalRecord.approval_id == request.approval_id,
                )
            ).first()
            if existing is not None:
                stored = _approval_from_row(existing)
                if (
                    stored.account_ref != request.account_ref
                    or stored.platform != request.platform
                    or stored.owner_ref != request.owner_ref
                    or stored.private_test_ref != request.private_test_ref
                ):
                    raise ValueError(
                        f"owner approval already granted with different fields: "
                        f"{request.approval_id}"
                    )
                return stored

            row = OwnerApprovalRecord(
                approval_id=request.approval_id,
                org_id=request.org_id,
                platform=request.platform,
                account_ref=request.account_ref,
                status=ApprovalStatus.GRANTED.value,
                owner_ref=request.owner_ref,
                private_test_ref=request.private_test_ref,
                note=request.note,
                granted_at=now,
                expires_at=request.expires_at,
            )
            session.add(row)
            session.flush()
            audit.record(
                "owner_approvals.granted",
                "public-gate-store",
                f"approval:{request.approval_id}",
                {
                    "org_id": request.org_id,
                    "platform": request.platform,
                    "account_ref": request.account_ref,
                    "owner_ref": request.owner_ref,
                    "private_test_ref": request.private_test_ref,
                },
            )
            return _approval_from_row(row)

    def revoke_approval(self, approval_id: str, request: RevokeApprovalRequest) -> OwnerApproval:
        """Revoke one approval (idempotent once revoked)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(OwnerApprovalRecord).where(
                    OwnerApprovalRecord.org_id == request.org_id,
                    OwnerApprovalRecord.approval_id == approval_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"owner approval not found: {approval_id}")
            if row.status == ApprovalStatus.REVOKED.value:
                return _approval_from_row(row)
            row.status = ApprovalStatus.REVOKED.value
            row.revoked_at = now
            session.flush()
            audit.record(
                "owner_approvals.revoked",
                "public-gate-store",
                f"approval:{approval_id}",
                {
                    "org_id": request.org_id,
                    "account_ref": row.account_ref,
                    "owner_ref": request.owner_ref,
                    "note": request.note,
                },
            )
            return _approval_from_row(row)

    def list_approvals(self, org_id: str) -> list[OwnerApproval]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(OwnerApprovalRecord)
                .where(OwnerApprovalRecord.org_id == org_id)
                .order_by(OwnerApprovalRecord.id)
            ).all()
            return [_approval_from_row(row) for row in rows]

    # -- private tests -----------------------------------------------------

    def record_private_test(self, request: RecordPrivateTestRequest) -> PrivateTest:
        """Idempotent per (org, test_ref)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(PrivateTestRecord).where(
                    PrivateTestRecord.org_id == request.org_id,
                    PrivateTestRecord.test_ref == request.test_ref,
                )
            ).first()
            if existing is not None:
                stored = _test_from_row(existing)
                if (
                    stored.account_ref != request.account_ref
                    or stored.outcome is not request.outcome
                ):
                    raise ValueError(
                        f"private test already recorded with different fields: {request.test_ref}"
                    )
                return stored

            row = PrivateTestRecord(
                test_ref=request.test_ref,
                org_id=request.org_id,
                platform=request.platform,
                account_ref=request.account_ref,
                outcome=request.outcome.value,
                detail=request.detail,
                recorded_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "private_tests.recorded",
                "public-gate-store",
                f"private_test:{request.test_ref}",
                {
                    "org_id": request.org_id,
                    "platform": request.platform,
                    "account_ref": request.account_ref,
                    "outcome": request.outcome.value,
                },
            )
            return _test_from_row(row)

    def list_private_tests(self, org_id: str) -> list[PrivateTest]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(PrivateTestRecord)
                .where(PrivateTestRecord.org_id == org_id)
                .order_by(PrivateTestRecord.id)
            ).all()
            return [_test_from_row(row) for row in rows]

    # -- decisions ---------------------------------------------------------

    def decide(self, org_id: str, account_ref: str, now: datetime | None = None) -> GateResult:
        return evaluate_public_gate(
            self.list_approvals(org_id),
            self.list_private_tests(org_id),
            account_ref,
            now or datetime.now(UTC),
        )

    def summary(self, org_id: str, now: datetime | None = None) -> GateSummary:
        return gate_summary(
            org_id,
            self.list_approvals(org_id),
            self.list_private_tests(org_id),
            now or datetime.now(UTC),
        )


router = APIRouter(tags=["publishing"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> PublicGateStore:
    return PublicGateStore.from_engine(engine)


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


@router.post(
    "/publishing/owner-approvals",
    response_model=OwnerApproval,
    name="grant_owner_approval",
)
def grant_approval_route(
    request: GrantApprovalRequest,
    store: Annotated[PublicGateStore, Depends(_store)],
) -> OwnerApproval:
    """Grant one owner approval for an account (idempotent)."""
    try:
        return store.grant_approval(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/publishing/owner-approvals",
    response_model=list[OwnerApproval],
    name="list_owner_approvals",
)
def list_approvals_route(
    store: Annotated[PublicGateStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[OwnerApproval]:
    """List owner approvals for one org."""
    return store.list_approvals(org_id)


@router.post(
    "/publishing/owner-approvals/{approval_id}/revoke",
    response_model=OwnerApproval,
    name="revoke_owner_approval",
)
def revoke_approval_route(
    approval_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: RevokeApprovalRequest,
    store: Annotated[PublicGateStore, Depends(_store)],
) -> OwnerApproval:
    """Revoke one owner approval (idempotent)."""
    try:
        return store.revoke_approval(approval_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post("/publishing/private-tests", response_model=PrivateTest, name="record_private_test")
def record_private_test_route(
    request: RecordPrivateTestRequest,
    store: Annotated[PublicGateStore, Depends(_store)],
) -> PrivateTest:
    """Record a private-test outcome for an account (idempotent)."""
    try:
        return store.record_private_test(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/publishing/private-tests", response_model=list[PrivateTest], name="list_private_tests"
)
def list_private_tests_route(
    store: Annotated[PublicGateStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[PrivateTest]:
    """List private tests for one org."""
    return store.list_private_tests(org_id)


@router.get("/publishing/gate-summary", response_model=GateSummary, name="public_gate_summary")
def gate_summary_route(
    store: Annotated[PublicGateStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> GateSummary:
    """Per-account public-publishing readiness for one org."""
    return store.summary(org_id)


__all__ = [
    "AccountGateReadiness",
    "ApprovalStatus",
    "GateDecision",
    "GateReason",
    "GateResult",
    "GateSummary",
    "GrantApprovalRequest",
    "OwnerApproval",
    "PrivateTest",
    "PrivateTestOutcome",
    "PublicGateStore",
    "RecordPrivateTestRequest",
    "RevokeApprovalRequest",
    "evaluate_public_gate",
    "gate_summary",
    "router",
]
