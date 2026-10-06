"""Operator visibility snapshot over outbox, idempotency, and audit state (M3).

``OperatorSnapshot.build`` performs only parameterized SQL reads on the supplied
database engine or session. It never opens a network connection: Temporal
reachability is supplied by the caller. Read-only snapshots are not audit
events by design.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from clipos_api.idempotency import IdempotencyRecordRecord, IdempotencyState
from clipos_api.outbox import OutboxEventRecord, OutboxStatus
from clipos_api.repository import AuditEventRecord


class OutboxVisibility(BaseModel):
    """Outbox status counts and the age of the oldest still-pending event."""

    model_config = ConfigDict(frozen=True)

    counts: dict[str, int]
    oldest_pending_age_seconds: float


class IdempotencyVisibility(BaseModel):
    """Idempotency state counts and expired in-progress locks."""

    model_config = ConfigDict(frozen=True)

    counts: dict[str, int]
    stale_locks: int


class OperatorAuditEvent(BaseModel):
    """A bounded audit tail entry; identifiers and outcomes only."""

    model_config = ConfigDict(frozen=True)

    event_type: str
    at: datetime
    outcome: str | None


class OperatorSnapshot(BaseModel):
    """Read-only operator health snapshot; generation time is explicit."""

    model_config = ConfigDict(frozen=True)

    outbox: OutboxVisibility
    idempotency: IdempotencyVisibility
    audit_events: list[OperatorAuditEvent]
    temporal_reachable: bool
    generated_at: datetime

    @classmethod
    def build(
        cls,
        engine_or_session: Engine | Session,
        now: datetime,
        last_n: int = 20,
        temporal_reachable: bool = False,
    ) -> OperatorSnapshot:
        """Build a bounded snapshot with parameterized SQL only.

        The audit tail is returned oldest-to-newest. ``temporal_reachable`` is
        never probed here; the caller is responsible for its meaning.
        """
        if last_n < 1:
            raise ValueError("last_n must be >= 1")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        cutoff = now.astimezone(UTC)
        with _session(engine_or_session) as session:
            outbox_rows = session.execute(
                sa.select(OutboxEventRecord.status, sa.func.count()).group_by(
                    OutboxEventRecord.status
                )
            ).all()
            oldest_pending = session.scalar(
                sa.select(sa.func.min(OutboxEventRecord.available_at)).where(
                    OutboxEventRecord.status == OutboxStatus.PENDING.value
                )
            )
            idempotency_rows = session.execute(
                sa.select(IdempotencyRecordRecord.state, sa.func.count()).group_by(
                    IdempotencyRecordRecord.state
                )
            ).all()
            stale_locks = int(
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(IdempotencyRecordRecord)
                    .where(
                        IdempotencyRecordRecord.state == IdempotencyState.IN_PROGRESS.value,
                        IdempotencyRecordRecord.locked_until < cutoff,
                    )
                )
                or 0
            )
            audit_rows = list(
                reversed(
                    session.scalars(
                        sa.select(AuditEventRecord)
                        .order_by(AuditEventRecord.seq.desc())
                        .limit(last_n)
                    ).all()
                )
            )
        outbox_counts = {status.value: 0 for status in OutboxStatus}
        for status, count in outbox_rows:
            outbox_counts[OutboxStatus(str(status))] = int(count)
        idempotency_counts = {state.value: 0 for state in IdempotencyState}
        for state, count in idempotency_rows:
            idempotency_counts[IdempotencyState(str(state))] = int(count)
        if oldest_pending is not None:
            oldest_pending = _aware(oldest_pending)
            oldest_pending_age = max(0.0, (cutoff - oldest_pending).total_seconds())
        else:
            oldest_pending_age = 0.0
        audit_events = [
            OperatorAuditEvent(
                event_type=row.action,
                at=_aware(row.timestamp),
                outcome=_outcome(row.details),
            )
            for row in audit_rows
        ]
        return cls(
            outbox=OutboxVisibility(
                counts=outbox_counts,
                oldest_pending_age_seconds=oldest_pending_age,
            ),
            idempotency=IdempotencyVisibility(
                counts=idempotency_counts,
                stale_locks=stale_locks,
            ),
            audit_events=audit_events,
            temporal_reachable=temporal_reachable,
            generated_at=cutoff,
        )


@contextmanager
def _session(value: Engine | Session) -> Iterator[Session]:
    if isinstance(value, Session):
        yield value
        return
    sessions = sessionmaker(bind=value, expire_on_commit=False)
    with sessions() as session:
        yield session


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _outcome(details: dict[str, Any] | None) -> str | None:
    """Extract the audit outcome without exposing raw details."""
    if not details:
        return None
    outcome = details.get("outcome")
    if isinstance(outcome, str):
        return outcome
    if details.get("terminal") is True:
        return "terminal_failed"
    return None
