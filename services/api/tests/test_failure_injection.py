"""Offline outbox and idempotency failure-injection tests (CI lane 8)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from clipos_api import audit, idempotency, outbox


def now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "failure-audit.log"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    return path


@pytest.fixture()
def outbox_store(audit_log: Path) -> outbox.OutboxStore:
    engine = create_engine("sqlite:///:memory:")
    outbox.OutboxEventRecord.metadata.create_all(engine)
    return outbox.OutboxStore.from_engine(engine)


@pytest.fixture()
def idempotency_store(audit_log: Path) -> idempotency.IdempotencyStore:
    engine = create_engine("sqlite:///:memory:")
    idempotency.IdempotencyRecordRecord.metadata.create_all(engine)
    return idempotency.IdempotencyStore.from_engine(engine)


def event(event_id: str = "evt-1") -> outbox.OutboxEvent:
    return outbox.OutboxEvent(
        id=event_id,
        aggregate_type="publication",
        aggregate_id="pub-1",
        event_type="publication.scheduled",
        payload={"content": "never-logged"},
        business_key="publish:a:v:s",
        available_at=now(),
        created_at=now(),
    )


def test_crashed_outbox_claim_recovers_after_expired_lease(
    outbox_store: outbox.OutboxStore,
) -> None:
    first = outbox_store.from_engine.__func__  # type: ignore[attr-defined]
    assert first is not None
    outbox_store.enqueue_in_transaction(event())
    crashed_claims = outbox_store.claim_batch(10, 60, now())
    assert [item.id for item in crashed_claims] == ["evt-1"]
    assert outbox_store.claim_batch(10, 60, now() + timedelta(seconds=1)) == []
    # A NEW store over the same engine simulates a replacement worker process;
    # the original transaction was abandoned without mark_published.
    replacement = outbox.OutboxStore(outbox_store._sessions)
    recovered = replacement.claim_batch(10, 60, now() + timedelta(seconds=61))
    assert [item.id for item in recovered] == ["evt-1"]
    assert recovered[0].attempts == 2


def test_delivery_exception_backoff_and_terminal_failed(
    outbox_store: outbox.OutboxStore, audit_log: Path
) -> None:
    with outbox_store._sessions.begin() as session:
        outbox_store.enqueue(session, event())
    clock = now()
    failures: list[outbox.OutboxEvent] = []
    for _ in range(3):
        claimed = outbox_store.claim_batch(1, 60, clock)[0]
        failed = outbox_store.mark_failed(
            claimed, "provider unavailable", 30, clock, max_attempts=3
        )
        failures.append(failed)
        clock = failed.available_at
    assert failures[-1].status is outbox.OutboxStatus.FAILED
    assert failures[0].available_at == now() + timedelta(seconds=30)
    assert failures[1].available_at == now() + timedelta(seconds=90)
    assert outbox_store.claim_batch(10, 60, clock + timedelta(days=1)) == []
    records = [json.loads(line) for line in audit_log.read_text().splitlines()]
    failed_records = [record for record in records if record["action"] == "outbox.failed"]
    assert len(failed_records) == 3
    for attempt, record in enumerate(failed_records, start=1):
        assert record["subject"] == "evt-1"
        assert record["details"]["event_type"] == "publication.scheduled"
        assert record["details"]["attempts"] == attempt
        assert "never-logged" not in str(record)


def test_crashed_idempotency_claim_recovers_then_replays_without_execution(
    idempotency_store: idempotency.IdempotencyStore,
) -> None:
    assert (
        idempotency_store.claim("key-1", "publishing", "hash-1", None, 60, now()).status
        is idempotency.ClaimStatus.CLAIMED
    )
    cutoff = now() + timedelta(seconds=61)
    assert idempotency_store.recover_stale(60, cutoff) == 1
    reclaim = idempotency_store.claim("key-1", "publishing", "hash-1", None, 60, cutoff)
    assert reclaim.status is idempotency.ClaimStatus.CLAIMED
    stored = idempotency_store.get("key-1")
    assert stored is not None and stored.attempts == 2
    assert idempotency_store.complete("key-1", {"result": "written"}, cutoff)
    calls = 0

    def fn() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"unexpected": "true"}

    assert idempotency.execute_idempotent(
        idempotency_store,
        "key-1",
        "publishing",
        "hash-1",
        fn,
        business_key="publish:a:v:s",
        now=cutoff,
    ) == {"result": "written"}
    assert calls == 0


def test_duplicate_outbox_delivery_has_exactly_one_consumer_side_effect(
    outbox_store: outbox.OutboxStore, idempotency_store: idempotency.IdempotencyStore
) -> None:
    outbox_store.enqueue_in_transaction(event())
    calls = 0

    def consumer() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"delivered": True}

    # Claim once: at-least-once delivery redelivers the SAME event; the consumer
    # dedupe (idempotency key) must make the side effect exactly once.
    claimed = outbox_store.claim_batch(1, 60, now())[0]
    for _ in range(2):
        idempotency.execute_idempotent(
            idempotency_store,
            key=claimed.id,
            scope="outbox-consumer",
            request_hash=idempotency.request_digest(claimed.payload),
            fn=consumer,
            business_key=claimed.business_key,
            now=now(),
        )
    assert calls == 1
