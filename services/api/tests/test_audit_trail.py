"""Hash-chained audit trail tests (CLIP-009)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from clipos_api.audit_trail import (
    AuditEvent,
    AuditTrail,
    InMemoryAuditTrailStore,
    canonical_json,
    compute_event_hash,
)


def now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_genesis_and_append_verify_intact() -> None:
    store = InMemoryAuditTrailStore()
    trail = AuditTrail(store, clock=now)
    e1 = trail.append("test.action", "tester", "obj-1", {"key": "value"})
    assert e1.seq == 1
    assert e1.prev_hash == ""
    e2 = trail.append("test.action", "tester", "obj-2", {"key": "value2"})
    assert e2.seq == 2
    assert e2.prev_hash == e1.event_hash
    assert trail.verify() == []


def test_canonical_deterministic_hash() -> None:
    details: dict[str, Any] = {"b": 2, "a": 1}
    h1 = compute_event_hash(1, "2026-01-01T12:00:00+00:00", "act", "actor", "subj", details, "")
    h2 = compute_event_hash(1, "2026-01-01T12:00:00+00:00", "act", "actor", "subj", details, "")
    assert h1 == h2
    assert canonical_json(details) == '{"a":1,"b":2}'


def test_tamper_details_reports_hash_mismatch() -> None:
    store = InMemoryAuditTrailStore()
    trail = AuditTrail(store, clock=now)
    trail.append("act", "actor", "subj", {"original": True})
    trail.append("act", "actor", "subj", {"step": 2})
    # Simulate tampering by replacing a stored event with modified details.
    tampered = store._events[0].model_copy(update={"details": {"original": False}})
    store._events[0] = tampered
    problems = trail.verify()
    assert any("hash mismatch at 1" in p for p in problems)


def test_delete_middle_reports_seq_gap_and_detached() -> None:
    store = InMemoryAuditTrailStore()
    trail = AuditTrail(store, clock=now)
    trail.append("act", "actor", "subj", {"step": 1})
    trail.append("act", "actor", "subj", {"step": 2})
    trail.append("act", "actor", "subj", {"step": 3})
    del store._events[1]  # remove seq 2
    problems = trail.verify()
    assert any("seq gap at 2" in p for p in problems)
    assert any("detached event at 3" in p for p in problems)


def test_reorder_reports_prev_hash_mismatch() -> None:
    store = InMemoryAuditTrailStore()
    trail = AuditTrail(store, clock=now)
    trail.append("act", "actor", "subj", {"step": 1})
    trail.append("act", "actor", "subj", {"step": 2})
    trail.append("act", "actor", "subj", {"step": 3})
    store._events[1], store._events[2] = store._events[2], store._events[1]
    problems = trail.verify()
    assert any("prev_hash mismatch" in p for p in problems)


def test_duplicate_seq_rejected() -> None:
    store = InMemoryAuditTrailStore()
    trail = AuditTrail(store, clock=now)
    trail.append("act", "actor", "subj", {"step": 1})
    event = AuditEvent(
        seq=1,
        timestamp=now(),
        action="act",
        actor="actor",
        subject="subj",
        details={},
        prev_hash="",
        event_hash="a" * 64,
    )
    with pytest.raises(ValueError, match="duplicate seq"):
        store.append(event)


def test_filters() -> None:
    trail = AuditTrail(clock=now)
    trail.append("action.a", "alice", "subj-1", {})
    trail.append("action.b", "bob", "subj-2", {})
    trail.append("action.a", "bob", "subj-3", {})
    assert len(trail.events(action="action.a")) == 2
    assert len(trail.events(actor="bob")) == 2
    assert len(trail.events(action="action.a", actor="alice")) == 1
    assert len(trail.events()) == 3


def test_thread_safety_200_events() -> None:
    trail = AuditTrail(clock=now)
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        for i in range(25):
            trail.append("concurrent.append", "worker", f"item-{i}", {"thread": i})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = trail.events()
    assert len(events) == 200
    assert [e.seq for e in events] == list(range(1, 201))
    assert trail.verify() == []


def test_interface_immutability() -> None:
    store = InMemoryAuditTrailStore()
    assert hasattr(store, "update") is False
    assert hasattr(store, "delete") is False
    assert hasattr(store, "reverse") is False
    assert hasattr(store, "pop") is False
    assert hasattr(store, "clear") is False


def test_empty_action_rejected() -> None:
    trail = AuditTrail(clock=now)
    with pytest.raises(ValueError, match="required"):
        trail.append("", "actor", "subj", {})


def test_record_from_legacy_preserves_timestamp() -> None:
    trail = AuditTrail(clock=now)
    legacy = {
        "timestamp": "2025-06-15T08:00:00+00:00",
        "action": "legacy.action",
        "actor": "legacy-actor",
        "subject": "legacy-subj",
        "details": {"imported": True},
    }
    event = trail.record_from_legacy(legacy)
    assert event.seq == 1
    assert event.action == "legacy.action"
    assert event.timestamp == datetime(2025, 6, 15, 8, 0, 0, tzinfo=UTC)
    assert trail.verify() == []


def test_record_from_legacy_missing_key() -> None:
    trail = AuditTrail(clock=now)
    with pytest.raises(ValueError, match="missing required key"):
        trail.record_from_legacy({"action": "incomplete"})


def test_invalid_event_rejected() -> None:
    with pytest.raises(ValidationError):
        AuditEvent(
            seq=0,
            timestamp=now(),
            action="act",
            actor="actor",
            subject="subj",
            details={},
            event_hash="a" * 64,
        )
