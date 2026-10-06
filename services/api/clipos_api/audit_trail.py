"""Append-only, hash-chained audit trail: tamper-evident event ledger.

Every event embeds a monotonically increasing ``seq``, the hash of its immediate
predecessor (``prev_hash``), and the SHA-256 of its canonical payload
(``event_hash``).  Chain verification detects modification, deletion, and
reordering.  PostgreSQL persistence lands in CLIP-010; this module ships the
store protocol plus a thread-safe in-memory implementation whose public
interface is append-only (no update, delete, reverse, pop, or clear).
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: dict[str, Any]) -> str:
    """Serialize ``value`` deterministically (sorted keys, compact separators)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def compute_event_hash(
    seq: int,
    timestamp: str,
    action: str,
    actor: str,
    subject: str,
    details: dict[str, Any],
    prev_hash: str,
) -> str:
    """Return the SHA-256 hex digest of the canonical event payload."""
    payload = f"{seq}|{timestamp}|{action}|{actor}|{subject}|{canonical_json(details)}|{prev_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditEvent(BaseModel):
    """A single hash-chained audit event."""

    model_config = ConfigDict(frozen=True)

    seq: int
    timestamp: datetime
    action: str
    actor: str
    subject: str
    details: dict[str, Any]
    prev_hash: str = ""
    event_hash: str

    @field_validator("seq")
    @classmethod
    def _seq_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("seq must be >= 1")
        return value

    @field_validator("action", "actor", "subject")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value:
            raise ValueError("value must not be empty")
        return value

    @field_validator("event_hash")
    @classmethod
    def _hash_present(cls, value: str) -> str:
        if not value:
            raise ValueError("event_hash must not be empty")
        return value

    @field_validator("timestamp")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value


@runtime_checkable
class AuditTrailStore(Protocol):
    """Persistence boundary for audit events; CLIP-010 provides the SQL store.

    The interface is deliberately append-only: there is no update, delete,
    reverse, pop, or clear.  Immutability is enforced by the public API.
    """

    def append(self, event: AuditEvent) -> AuditEvent:
        """Persist the event; return the stored event."""
        ...

    def events(self) -> list[AuditEvent]:
        """Return all stored events in chain order."""
        ...


class InMemoryAuditTrailStore:
    """Thread-safe in-memory AuditTrailStore backed by a private append-only list."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> AuditEvent:
        with self._lock:
            if any(existing.seq == event.seq for existing in self._events):
                raise ValueError(f"duplicate seq: {event.seq}")
            self._events.append(event)
            return event

    def events(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)


class AuditTrail:
    """Thread-safe append-only hash chain over an AuditTrailStore."""

    def __init__(
        self,
        store: AuditTrailStore | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store or InMemoryAuditTrailStore()
        self._clock = clock
        self._lock = threading.Lock()

    def append(
        self,
        action: str,
        actor: str,
        subject: str,
        details: dict[str, Any],
    ) -> AuditEvent:
        """Append a new event with the next seq and the current chain head hash."""
        if not action or not actor or not subject:
            raise ValueError("action, actor, and subject are required")
        return self._append_event(action, actor, subject, details, self._clock())

    def record_from_legacy(self, legacy_event: dict[str, Any]) -> AuditEvent:
        """Import a legacy ``clipos_api.audit`` record into the hash chain.

        The legacy dict must contain ``timestamp``, ``action``, ``actor``,
        ``subject``, and ``details``.  The original timestamp is preserved in
        the canonical payload so the chain hash covers the historical time.
        """
        try:
            timestamp_raw = legacy_event["timestamp"]
            action = legacy_event["action"]
            actor = legacy_event["actor"]
            subject = legacy_event["subject"]
            details = legacy_event["details"]
        except KeyError as exc:
            raise ValueError(f"legacy event missing required key: {exc}") from exc
        timestamp = datetime.fromisoformat(timestamp_raw)
        return self._append_event(action, actor, subject, details, timestamp)

    def _append_event(
        self,
        action: str,
        actor: str,
        subject: str,
        details: dict[str, Any],
        timestamp: datetime,
    ) -> AuditEvent:
        with self._lock:
            existing = self._store.events()
            seq = existing[-1].seq + 1 if existing else 1
            prev_hash = existing[-1].event_hash if existing else ""
            ts_iso = timestamp.isoformat()
            event = AuditEvent(
                seq=seq,
                timestamp=timestamp,
                action=action,
                actor=actor,
                subject=subject,
                details=details,
                prev_hash=prev_hash,
                event_hash=compute_event_hash(
                    seq, ts_iso, action, actor, subject, details, prev_hash
                ),
            )
            return self._store.append(event)

    def verify(self) -> list[str]:
        """Return human-readable problems, or [] when the chain is intact."""
        problems: list[str] = []
        events = self._store.events()
        expected_seq = 1
        for i, event in enumerate(events):
            if event.seq != expected_seq:
                problems.append(f"seq gap at {expected_seq}")
                expected_seq = event.seq
            expected_seq = event.seq + 1

            recomputed = compute_event_hash(
                event.seq,
                event.timestamp.isoformat(),
                event.action,
                event.actor,
                event.subject,
                event.details,
                event.prev_hash,
            )
            if recomputed != event.event_hash:
                problems.append(f"hash mismatch at {event.seq}")

            if i == 0:
                if event.prev_hash != "":
                    problems.append(f"detached event at {event.seq}")
            elif event.prev_hash != events[i - 1].event_hash:
                if any(e.event_hash == event.prev_hash for e in events[:i]):
                    problems.append(f"prev_hash mismatch at {event.seq}")
                else:
                    problems.append(f"detached event at {event.seq}")
        return problems

    def events(
        self,
        action: str | None = None,
        actor: str | None = None,
    ) -> list[AuditEvent]:
        """Return events filtered by action and/or actor."""
        result = self._store.events()
        if action is not None:
            result = [e for e in result if e.action == action]
        if actor is not None:
            result = [e for e in result if e.actor == actor]
        return result
