"""Operator snapshot and route wiring tests (offline, parameterized SQL only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from clipos_api.idempotency import IdempotencyRecordRecord
from clipos_api.operator import OperatorSnapshot
from clipos_api.outbox import OutboxEvent, OutboxStore
from clipos_api.repository import AuditEventRecord, Base


def now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def engine() -> sa.Engine:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def outbox_event(event_id: str) -> OutboxEvent:
    return OutboxEvent(
        id=event_id,
        aggregate_type="publication",
        aggregate_id="pub-1",
        event_type="publication.scheduled",
        payload={"value": "safe"},
        business_key="publish:a:v:s",
        available_at=now(),
        created_at=now(),
    )


def test_operator_snapshot_counts_and_audit_tail(engine: sa.Engine) -> None:
    store = OutboxStore.from_engine(engine)
    evt1 = outbox_event("evt-1")
    store.enqueue_in_transaction(evt1.model_copy(update={"available_at": now() - timedelta(seconds=90)}))
    store.enqueue_in_transaction(outbox_event("evt-2").model_copy(update={"available_at": now()}))
    store.claim_batch(1, 60, now() - timedelta(seconds=300))
    with sa.orm.Session(engine) as session:
        session.add(
            IdempotencyRecordRecord(
                key="key-1",
                scope="publishing",
                state="in_progress",
                attempts=1,
                locked_until=now() - timedelta(seconds=1),
                created_at=now(),
                updated_at=now(),
            )
        )
        session.add(
            IdempotencyRecordRecord(
                key="key-2",
                scope="publishing",
                state="succeeded",
                attempts=1,
                response={"ok": True},
                created_at=now(),
                updated_at=now(),
            )
        )
        session.add(
            AuditEventRecord(
                seq=1,
                timestamp=now() - timedelta(seconds=2),
                action="outbox.enqueued",
                actor="outbox-store",
                subject="evt-1",
                details={"payload_hash": "x"},
                event_hash="x",
            )
        )
        session.add(
            AuditEventRecord(
                seq=2,
                timestamp=now(),
                action="workflow.self_test",
                actor="workflow-worker",
                subject="corr-1",
                details={"outcome": "approved"},
                event_hash="y",
            )
        )
        session.commit()
    snapshot = OperatorSnapshot.build(engine, now(), last_n=2, temporal_reachable=True)
    assert snapshot.outbox.counts == {"pending": 2, "published": 0, "failed": 0}
    assert snapshot.outbox.oldest_pending_age_seconds == 90
    assert snapshot.idempotency.counts == {"in_progress": 1, "succeeded": 1, "failed": 0}
    assert snapshot.idempotency.stale_locks == 1
    assert [event.event_type for event in snapshot.audit_events] == [
        "outbox.enqueued",
        "workflow.self_test",
    ]
    assert [event.outcome for event in snapshot.audit_events] == [None, "approved"]
    assert snapshot.temporal_reachable is True
    assert snapshot.generated_at == now()
    assert snapshot.model_dump_json()


def test_operator_snapshot_rejects_naive_time_and_zero_tail(engine: sa.Engine) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        OperatorSnapshot.build(engine, datetime(2026, 1, 1, 12, 0, 0))  # noqa: DTZ001 - intentionally naive
    with pytest.raises(ValueError, match="last_n"):
        OperatorSnapshot.build(engine, now(), last_n=0)


def test_operator_health_route_is_wired(engine: sa.Engine) -> None:
    from clipos_api.main import app, operator_health

    matches = [route for route in app.routes if getattr(route, "path", None) == "/operator/health"]
    assert matches, "no /operator/health route registered"
    assert matches[0].endpoint is operator_health
