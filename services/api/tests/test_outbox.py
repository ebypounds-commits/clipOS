"""Outbox unit tests: SQLite proves transitions; PG proves SKIP LOCKED."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from clipos_api import audit, outbox


def now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> outbox.OutboxStore:
    audit.AUDIT_PATH = tmp_path / "outbox-audit.log"
    audit.AUDIT_PATH.write_text("", encoding="utf-8")
    engine = create_engine("sqlite:///:memory:")
    outbox.OutboxEventRecord.metadata.create_all(engine)
    return outbox.OutboxStore.from_engine(engine)


def event(event_id: str = "evt-1", attempts: int = 0) -> outbox.OutboxEvent:
    return outbox.OutboxEvent(
        id=event_id,
        aggregate_type="publication",
        aggregate_id="pub-1",
        event_type="publication.scheduled",
        payload={"secret": "do-not-log"},
        business_key="publish:acct:variant:slot",
        available_at=now(),
        created_at=now(),
        attempts=attempts,
    )


def audit_events() -> list[dict[str, object]]:
    return audit.read_events()


def test_enqueue_claims_marks_roundtrip(store: outbox.OutboxStore) -> None:
    stored = store.enqueue_in_transaction(event())
    assert stored.status is outbox.OutboxStatus.PENDING
    claimed = store.claim_batch(10, 60, now())
    assert [item.id for item in claimed] == ["evt-1"]
    assert claimed[0].attempts == 1
    assert store.mark_published(["evt-1"], now()) == 1
    assert store.mark_published(["evt-1"], now()) == 0
    final = store.get("evt-1")
    assert final is not None
    assert final.status is outbox.OutboxStatus.PUBLISHED
    assert final.published_at == now()
    actions = [item["action"] for item in audit_events()]
    assert actions == ["outbox.enqueued", "outbox.published"]


def test_claim_respects_due_time_and_limit(store: outbox.OutboxStore) -> None:
    first = event("evt-1")
    second = event("evt-2").model_copy(update={"available_at": now() + timedelta(seconds=1)})
    with store._sessions.begin() as session:
        store.enqueue(session, first)
        store.enqueue(session, second)
    assert [item.id for item in store.claim_batch(10, 60, now())] == ["evt-1"]
    later = now() + timedelta(seconds=1)
    assert [item.id for item in store.claim_batch(10, 60, later)] == ["evt-2"]


def test_backoff_math_and_terminal_failure(store: outbox.OutboxStore) -> None:
    with store._sessions.begin() as session:
        store.enqueue(session, event())
    claimed = store.claim_batch(1, 60, now())[0]
    failed = store.mark_failed(claimed, "provider unavailable", 30, now(), 3)
    assert failed.status is outbox.OutboxStatus.PENDING
    assert failed.available_at == now() + timedelta(seconds=30)
    claimed = store.claim_batch(1, 60, failed.available_at)[0]
    assert claimed.attempts == 2
    failed = store.mark_failed(claimed, "still unavailable", 30, failed.available_at, 3)
    assert failed.available_at == now() + timedelta(seconds=90)
    claimed = store.claim_batch(1, 60, failed.available_at)[0]
    failed = store.mark_failed(claimed, "gave up", 30, failed.available_at, 3)
    assert failed.status is outbox.OutboxStatus.FAILED
    assert store.claim_batch(10, 60, now() + timedelta(days=1)) == []
    assert [item["action"] for item in audit_events()].count("outbox.failed") == 3


def test_stats_and_payload_never_in_audit(store: outbox.OutboxStore) -> None:
    with store._sessions.begin() as session:
        store.enqueue(session, event())
    assert store.stats() == {"pending": 1, "published": 0, "failed": 0}
    store.claim_batch(1, 60, now())
    store.mark_published(["evt-1"], now())
    for record in audit_events():
        text = str(record)
        assert "do-not-log" not in text
        assert record["details"]["payload_hash"]  # type: ignore[index]


def test_invalid_inputs(store: outbox.OutboxStore) -> None:
    with store._sessions.begin() as session:
        store.enqueue(session, event())
    with pytest.raises(ValueError, match="limit"):
        store.claim_batch(0, 60, now())
    claimed = store.claim_batch(1, 60, now())[0]
    with pytest.raises(ValueError, match="backoff"):
        store.mark_failed(claimed, "bad", 0, now(), 1)
    missing = event("missing")
    with pytest.raises(ValueError, match="not found"):
        store.mark_failed(missing, "bad", 1, now(), 1)
