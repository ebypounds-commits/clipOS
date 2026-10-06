"""Transactional outbox store and lifecycle primitives (M3).

The outbox is written in the *caller's* database transaction: ``enqueue`` joins
an existing SQLAlchemy session and never commits.  Callers commit the business
mutation and the event together, which is the transactional guarantee from
02_SYSTEM_ARCHITECTURE.md §9.  Delivery workers lease pending rows with
``FOR UPDATE SKIP LOCKED`` so concurrent workers claim disjoint batches.
Audit records contain identifiers and hashed/omitted payload metadata only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base


def payload_digest(payload: dict[str, Any]) -> str:
    """Return the SHA-256 of canonical payload JSON; never return the payload."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class OutboxStatus(str, Enum):
    """Outbox row states; the default is applied in code, not by the database."""

    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


class OutboxEvent(BaseModel):
    """Application-level outbox event mirroring ``outbox_events``."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=64)
    aggregate_type: str = Field(min_length=1, max_length=63)
    aggregate_id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=255)
    payload: dict[str, Any]
    business_key: str | None = Field(default=None, max_length=255)
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    available_at: datetime
    created_at: datetime
    published_at: datetime | None = None
    last_error: str | None = None

    @field_validator("available_at", "created_at", "published_at")
    @classmethod
    def _timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value


class OutboxEventRecord(Base):
    """SQLAlchemy row for ``outbox_events``; values are constrained text."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        sa.Index("ix_outbox_status_available", "status", "available_at"),
        sa.Index("ix_outbox_business_key", "business_key"),
    )

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(sa.String(63), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    business_key: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    available_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    published_at: Mapped[Any | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _record(event: OutboxEvent) -> OutboxEventRecord:
    return OutboxEventRecord(
        id=event.id,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        event_type=event.event_type,
        payload=event.payload,
        business_key=event.business_key,
        status=event.status.value,
        attempts=event.attempts,
        available_at=event.available_at,
        created_at=event.created_at,
        published_at=event.published_at,
        last_error=event.last_error,
    )


def _model(record: OutboxEventRecord) -> OutboxEvent:
    return OutboxEvent(
        id=record.id,
        aggregate_type=record.aggregate_type,
        aggregate_id=record.aggregate_id,
        event_type=record.event_type,
        payload=record.payload,
        business_key=record.business_key,
        status=OutboxStatus(record.status),
        attempts=record.attempts,
        available_at=_aware(record.available_at),
        created_at=_aware(record.created_at),
        published_at=_aware(record.published_at) if record.published_at else None,
        last_error=record.last_error,
    )


def _audit(action: str, event: OutboxEvent, **details: Any) -> None:
    safe: dict[str, Any] = {
        "event_type": event.event_type,
        "attempts": event.attempts,
        "payload_hash": payload_digest(event.payload),
    }
    if event.business_key:
        safe["business_key"] = event.business_key
    safe.update(details)
    audit.record(action, "outbox-store", event.id, safe)


class OutboxStore:
    """Session-bound outbox persistence; all SQL is parameterized."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> OutboxStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def enqueue(self, session: Session, event: OutboxEvent) -> OutboxEvent:
        """Add an event to the caller's transaction; the caller must commit.

        This method performs no commit/rollback: if the surrounding business
        transaction rolls back, the event disappears with it.  That shared
        atomic commit is the transactional outbox guarantee.
        """
        session.add(_record(event))
        session.flush()
        _audit("outbox.enqueued", event)
        return event

    def enqueue_in_transaction(self, event: OutboxEvent) -> OutboxEvent:
        """Convenience wrapper that opens one transaction and commits it.

        Use ``enqueue`` instead when an event must atomically accompany an
        existing business mutation on the caller's session/transaction.
        """
        with self._sessions.begin() as session:
            return self.enqueue(session, event)

    def claim_batch(self, limit: int, lease_seconds: int, now: datetime) -> list[OutboxEvent]:
        """Lease up to ``limit`` due pending rows, atomically incrementing attempts.

        PostgreSQL executes ``SELECT ... FOR UPDATE SKIP LOCKED``; the lock is
        released on commit.  SQLite does not implement row locking, so its unit
        tests prove state transitions, while the DATABASE_URL-gated test proves
        concurrent disjoint claims on real PostgreSQL.
        """
        with self._sessions.begin() as session:
            return self.claim_batch_in_transaction(session, limit, lease_seconds, now)

    def claim_batch_in_transaction(
        self, session: Session, limit: int, lease_seconds: int, now: datetime
    ) -> list[OutboxEvent]:
        """Lease due pending rows inside the caller's transaction.

        The claim performs ``SELECT ... FOR UPDATE SKIP LOCKED`` and increments
        attempts atomically, but neither commits nor rolls back.  Row locks stay
        held until the caller ends its transaction, allowing competing workers
        to prove disjoint claims without opening a new transaction.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be >= 1")
        cutoff = _aware(now)
        claimed: list[OutboxEvent] = []
        rows = session.scalars(
            sa.select(OutboxEventRecord)
            .where(
                OutboxEventRecord.status == OutboxStatus.PENDING.value,
                OutboxEventRecord.available_at <= cutoff,
            )
            .order_by(OutboxEventRecord.available_at, OutboxEventRecord.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            row.attempts += 1
            row.available_at = cutoff + timedelta(seconds=lease_seconds)
            claimed.append(_model(row))
        return claimed

    def mark_published(self, ids: list[str], now: datetime) -> int:
        """Mark the supplied ids published and audit each successful delivery."""
        if not ids:
            return 0
        cutoff = _aware(now)
        audited: list[OutboxEvent] = []
        with self._sessions.begin() as session:
            rows = session.scalars(
                sa.select(OutboxEventRecord).where(OutboxEventRecord.id.in_(ids)).with_for_update()
            ).all()
            for row in rows:
                if row.status != OutboxStatus.PUBLISHED.value:
                    row.status = OutboxStatus.PUBLISHED.value
                    row.published_at = cutoff
                    audited.append(_model(row))
        for event in audited:
            _audit("outbox.published", event)
        return len(audited)

    def mark_failed(
        self,
        event: OutboxEvent,
        error: str,
        retry_backoff_seconds: int,
        now: datetime,
        max_attempts: int,
    ) -> OutboxEvent:
        """Schedule exponential retry or mark the event terminally failed."""
        if retry_backoff_seconds < 1:
            raise ValueError("retry_backoff_seconds must be >= 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        cutoff = _aware(now)
        terminal = event.attempts >= max_attempts
        next_available = None
        next_status = OutboxStatus.FAILED
        if not terminal:
            backoff = timedelta(seconds=retry_backoff_seconds * (2 ** (event.attempts - 1)))
            next_available = cutoff + backoff
            next_status = OutboxStatus.PENDING
        with self._sessions.begin() as session:
            row = session.get(OutboxEventRecord, event.id)
            if row is None:
                raise ValueError(f"outbox event not found: {event.id}")
            row.status = next_status.value
            row.last_error = error
            if next_available is not None:
                row.available_at = next_available
            updated = _model(row)
        _audit(
            "outbox.failed",
            updated,
            error_class="delivery_error",
            terminal=terminal,
            retry_at=next_available.isoformat() if next_available else None,
        )
        return updated

    def stats(self) -> dict[str, int]:
        """Return operator-visible counts by status (all three keys present)."""
        with self._sessions() as session:
            rows = session.execute(
                sa.select(OutboxEventRecord.status, sa.func.count()).group_by(
                    OutboxEventRecord.status
                )
            ).all()
        counts = {status.value: 0 for status in OutboxStatus}
        for status, count in rows:
            counts[OutboxStatus(status)] = int(count)
        return counts

    def get(self, event_id: str) -> OutboxEvent | None:
        """Return one event or ``None`` (test/helper convenience)."""
        with self._sessions() as session:
            row = session.get(OutboxEventRecord, event_id)
            return _model(row) if row is not None else None
