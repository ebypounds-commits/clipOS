"""Idempotency unit tests: claim, replay, lease contention, and recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from clipos_api import audit, idempotency
from clipos_api.idempotency import ClaimStatus


def now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> idempotency.IdempotencyStore:
    audit.AUDIT_PATH = Path("/tmp/clipos-016-idempotency-audit.log")
    audit.AUDIT_PATH.write_text("", encoding="utf-8")
    engine = create_engine("sqlite:///:memory:")
    idempotency.IdempotencyRecordRecord.metadata.create_all(engine)
    return idempotency.IdempotencyStore.from_engine(engine)


def audit_actions() -> list[str]:
    return [item["action"] for item in audit.read_events()]


def test_claim_complete_replay_does_not_call_fn(store: idempotency.IdempotencyStore) -> None:
    calls = 0

    def fn() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"provider_id": "ext-123"}

    result = idempotency.execute_idempotent(
        store, "key-1", "publishing", "hash-1", fn, business_key="publish:a:v:s", now=now()
    )
    assert result == {"provider_id": "ext-123"}
    replay = idempotency.execute_idempotent(store, "key-1", "publishing", "hash-1", fn, now=now())
    assert replay == result
    assert calls == 1
    assert audit_actions() == [
        "idempotency.claimed",
        "idempotency.completed",
        "idempotency.replayed",
    ]
    stored = store.get("key-1")
    assert stored is not None
    assert stored.attempts == 1
    assert stored.locked_until is None


def test_request_hash_mismatch_on_succeeded_key(
    store: idempotency.IdempotencyStore,
) -> None:
    idempotency.execute_idempotent(
        store, "key-1", "publishing", "hash-1", lambda: {"ok": True}, now=now()
    )
    with pytest.raises(ValueError, match="request hash mismatch"):
        idempotency.execute_idempotent(
            store, "key-1", "publishing", "hash-2", lambda: {"ok": True}, now=now()
        )


def test_in_progress_raises_and_does_not_execute(store: idempotency.IdempotencyStore) -> None:
    first = store.claim("key-1", "publishing", "hash-1", None, 300, now())
    assert first.status is ClaimStatus.CLAIMED
    contention = store.claim("key-1", "publishing", "hash-1", None, 300, now())
    assert contention.status is ClaimStatus.IN_PROGRESS
    with pytest.raises(idempotency.IdempotencyInProgress):
        idempotency.execute_idempotent(
            store, "key-1", "publishing", "hash-1", lambda: {"unexpected": True}, now=now()
        )


def test_failed_reclaims_with_attempt_increment(store: idempotency.IdempotencyStore) -> None:
    def failing() -> dict[str, object]:
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        idempotency.execute_idempotent(store, "key-1", "publishing", "hash-1", failing, now=now())
    result = idempotency.execute_idempotent(
        store, "key-1", "publishing", "hash-1", lambda: {"ok": True}, now=now()
    )
    assert result == {"ok": True}
    stored = store.get("key-1")
    assert stored is not None
    assert stored.state is idempotency.IdempotencyState.SUCCEEDED
    assert stored.attempts == 2


def test_expired_lease_and_recover_stale(store: idempotency.IdempotencyStore) -> None:
    store.claim("key-1", "publishing", "hash-1", None, 60, now())
    cutoff = now() + timedelta(seconds=61)
    assert store.recover_stale(60, cutoff) == 1
    reclaim = store.claim("key-1", "publishing", "hash-1", None, 60, cutoff)
    assert reclaim.status is ClaimStatus.CLAIMED
    stored = store.get("key-1")
    assert stored is not None
    assert stored.attempts == 2


def test_scope_and_input_validation(store: idempotency.IdempotencyStore) -> None:
    assert (
        store.claim("key-1", "publishing", "hash-1", None, 60, now()).status is ClaimStatus.CLAIMED
    )
    with pytest.raises(ValueError, match="different scope"):
        store.claim("key-1", "render", "hash-1", None, 60, now())
    with pytest.raises(ValueError, match="lock_seconds"):
        store.claim("key-2", "render", "hash-1", None, 0, now())
