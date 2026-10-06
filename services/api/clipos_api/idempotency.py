"""Durable idempotency records for external writes (M3).

A record is claimed with ``INSERT ... ON CONFLICT DO NOTHING``; rowcount is the
race-safe authority.  Every later state transition is a single conditional
UPDATE, so there is no multi-step crash window.  Responses are persisted only
after the wrapped function returns; failed records are re-claimable with an
attempt increment.  Business keys reuse the formats from
``clipos_api.workflow_framework.business_key`` and are never reformatted here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base


def request_digest(request: dict[str, Any] | None) -> str:
    """Hash canonical request JSON; raw request content is never stored/logged."""
    if request is None:
        return ""
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class IdempotencyState(str, Enum):
    """Record states: pending work is in progress; succeeded is replayable."""

    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ClaimStatus(str, Enum):
    """Outcome of a race-safe claim."""

    CLAIMED = "claimed"
    ALREADY_SUCCEEDED = "already_succeeded"
    IN_PROGRESS = "in_progress"
    FAILED_PREVIOUS = "failed_previous"


class ClaimResult(BaseModel):
    """Claim outcome with optional replay response or lock deadline."""

    model_config = ConfigDict(frozen=True)

    status: ClaimStatus
    response: dict[str, Any] | None = None
    locked_until: datetime | None = None

    @field_validator("locked_until")
    @classmethod
    def _timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("locked_until must be timezone-aware")
        return value


class IdempotencyInProgress(RuntimeError):
    """Raised by ``execute_idempotent`` when another worker holds the key."""

    def __init__(self, key: str, locked_until: datetime) -> None:
        self.key = key
        self.locked_until = locked_until
        super().__init__(f"idempotency key in progress: {key} until {locked_until.isoformat()}")


class IdempotencyRecord(BaseModel):
    """Application-level idempotency record mirroring the table."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1, max_length=255)
    scope: str = Field(min_length=1, max_length=64)
    state: IdempotencyState
    request_hash: str | None = Field(default=None, max_length=64)
    response: dict[str, Any] | None = None
    business_key: str | None = Field(default=None, max_length=255)
    attempts: int = Field(default=1, ge=1)
    locked_until: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("locked_until", "created_at", "updated_at")
    @classmethod
    def _timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value


class IdempotencyRecordRecord(Base):
    """SQLAlchemy row for ``idempotency_records``."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        sa.Index(
            "ix_idempotency_records_business_key",
            "business_key",
            unique=True,
            postgresql_where=sa.text("business_key IS NOT NULL"),
        ),
        sa.Index("ix_idempotency_records_state_locked", "state", "locked_until"),
    )

    key: Mapped[str] = mapped_column(sa.String(255), primary_key=True)
    scope: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    request_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    response: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    business_key: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    locked_until: Mapped[Any | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _model(row: IdempotencyRecordRecord) -> IdempotencyRecord:
    return IdempotencyRecord(
        key=row.key,
        scope=row.scope,
        state=IdempotencyState(row.state),
        request_hash=row.request_hash,
        response=row.response,
        business_key=row.business_key,
        attempts=row.attempts,
        locked_until=_aware(row.locked_until) if row.locked_until else None,
        last_error=row.last_error,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def _audit(action: str, key: str, scope: str, **details: Any) -> None:
    audit.record(action, "idempotency-store", key, {"scope": scope, **details})


class IdempotencyStore:
    """Session-bound idempotency persistence; every transition is atomic SQL."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> IdempotencyStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def claim(
        self,
        key: str,
        scope: str,
        request_hash: str,
        business_key: str | None,
        lock_seconds: int,
        now: datetime,
    ) -> ClaimResult:
        """Race-safe claim using INSERT ON CONFLICT DO NOTHING and rowcount."""
        if lock_seconds < 1:
            raise ValueError("lock_seconds must be >= 1")
        cutoff = _aware(now)
        deadline = _aware(now + timedelta(seconds=lock_seconds))
        inserted = False
        with self._sessions.begin() as session:
            dialect_insert = (
                pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert  # type: ignore[union-attr]
            )
            result = session.execute(
                dialect_insert(IdempotencyRecordRecord)
                .values(
                    key=key,
                    scope=scope,
                    state=IdempotencyState.IN_PROGRESS.value,
                    request_hash=request_hash,
                    business_key=business_key,
                    attempts=1,
                    locked_until=deadline,
                    created_at=cutoff,
                    updated_at=cutoff,
                )
                .on_conflict_do_nothing(index_elements=[IdempotencyRecordRecord.key])
            )
            inserted = bool(result.rowcount)  # type: ignore[attr-defined]
            row = session.scalars(
                sa.select(IdempotencyRecordRecord).where(IdempotencyRecordRecord.key == key)
            ).one()
            if not inserted:
                if row.scope != scope:
                    raise ValueError(f"idempotency key belongs to a different scope: {key}")
                if row.state == IdempotencyState.SUCCEEDED.value:
                    if row.request_hash and row.request_hash != request_hash:
                        raise ValueError("request hash mismatch for succeeded idempotency key")
                    return ClaimResult(
                        status=ClaimStatus.ALREADY_SUCCEEDED,
                        response=row.response,
                    )
                if row.state == IdempotencyState.IN_PROGRESS.value:
                    locked_until = _aware(row.locked_until) if row.locked_until else cutoff
                    if locked_until > cutoff:
                        return ClaimResult(
                            status=ClaimStatus.IN_PROGRESS, locked_until=locked_until
                        )
                # A failed record, or an expired in-progress lease, is reclaimable.
                row.state = IdempotencyState.IN_PROGRESS.value
                row.request_hash = request_hash
                row.business_key = business_key
                row.attempts += 1
                row.locked_until = deadline
                row.updated_at = cutoff
        event_action = "idempotency.claimed"
        _audit(event_action, key, scope, attempt=row.attempts)
        return ClaimResult(status=ClaimStatus.CLAIMED)

    def complete(self, key: str, response: dict[str, Any], now: datetime) -> bool:
        """Atomically persist the success response and release the lock."""
        cutoff = _aware(now)
        with self._sessions.begin() as session:
            row = session.get(IdempotencyRecordRecord, key)
            if row is None:
                return False
            if row.state == IdempotencyState.SUCCEEDED.value:
                return False
            row.state = IdempotencyState.SUCCEEDED.value
            row.response = response
            row.locked_until = None
            row.updated_at = cutoff
            scope = row.scope
        _audit("idempotency.completed", key, scope, response_hash=request_digest(response))
        return True

    def fail(self, key: str, error: str, now: datetime) -> bool:
        """Atomically mark failed; the next claim increments attempts."""
        cutoff = _aware(now)
        with self._sessions.begin() as session:
            result = session.execute(
                sa.update(IdempotencyRecordRecord)
                .where(
                    IdempotencyRecordRecord.key == key,
                    IdempotencyRecordRecord.state != IdempotencyState.SUCCEEDED.value,
                )
                .values(
                    state=IdempotencyState.FAILED.value,
                    last_error=error,
                    locked_until=None,
                    updated_at=cutoff,
                )
            )
            updated = bool(result.rowcount)  # type: ignore[attr-defined]
        return updated

    def recover_stale(self, lock_seconds: int, now: datetime) -> int:
        """Reclaimable records: expired in-progress leases become pending-like.

        The state remains ``in_progress`` until a claimant takes it, but the
        expired ``locked_until`` is the recoverable marker checked by ``claim``.
        """
        if lock_seconds < 1:
            raise ValueError("lock_seconds must be >= 1")
        cutoff = _aware(now)
        with self._sessions.begin() as session:
            result = session.execute(
                sa.update(IdempotencyRecordRecord)
                .where(
                    IdempotencyRecordRecord.state == IdempotencyState.IN_PROGRESS.value,
                    IdempotencyRecordRecord.locked_until < cutoff,
                )
                .values(updated_at=cutoff)
            )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    def get(self, key: str) -> IdempotencyRecord | None:
        """Return one record or ``None`` (test/helper convenience)."""
        with self._sessions() as session:
            row = session.get(IdempotencyRecordRecord, key)
            return _model(row) if row is not None else None


def execute_idempotent(
    store: IdempotencyStore,
    key: str,
    scope: str,
    request_hash: str,
    fn: Callable[[], dict[str, Any]],
    lock_seconds: int = 300,
    business_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim, execute, and complete; replay without executing on success.

    A held in-progress lease raises ``IdempotencyInProgress`` so the caller can
    retry later.  If ``fn`` raises, the record is failed and the exception is
    re-raised; the next execution re-claims with ``attempts += 1``.
    """
    current = now or datetime.now(UTC)
    result = store.claim(key, scope, request_hash, business_key, lock_seconds, current)
    if result.status is ClaimStatus.ALREADY_SUCCEEDED:
        audit.record(
            "idempotency.replayed",
            "idempotency-store",
            key,
            {"scope": scope, "response_hash": request_digest(result.response)},
        )
        if result.response is None:
            raise ValueError(f"succeeded idempotency record has no response: {key}")
        return result.response
    if result.status is ClaimStatus.IN_PROGRESS:
        if result.locked_until is None:
            raise ValueError("in-progress claim is missing its deadline")
        raise IdempotencyInProgress(key, result.locked_until)
    try:
        response = fn()
    except BaseException as error:
        store.fail(key, type(error).__name__, current)
        raise
    if not store.complete(key, response, current):
        raise ValueError(f"idempotency record disappeared: {key}")
    return response
