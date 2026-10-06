"""Tests for the identity/session foundation (CLIP-006)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from clipos_api import audit
from clipos_api.identity import (
    IdentityService,
    InMemorySessionStore,
    Session,
    SessionState,
    User,
    UserStatus,
    cookie_attributes,
    hash_token,
    new_csrf_token,
)

TTL = timedelta(minutes=10)


class Clock:
    """Injectable test clock: no sleeps."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture()
def audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "audit.log"
    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    return path


def make_service(clock: Clock) -> IdentityService:
    return IdentityService(InMemorySessionStore(), clock=clock)


def make_user(**overrides: object) -> User:
    fields: dict[str, object] = {
        "id": "user-1",
        "email": "alice@example.com",
        "subject": "alice",
        "display_name": "Alice",
        "status": UserStatus.ACTIVE,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return User(**fields)  # type: ignore[arg-type]


def test_user_rejects_bad_email_and_bad_status() -> None:
    with pytest.raises(ValidationError):
        make_user(email="not-an-email")
    with pytest.raises(ValidationError):
        make_user(status="archived")  # type: ignore[arg-type]


def test_session_rejects_bad_state() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        Session(
            id="s1",
            user_id="u1",
            token_hash="h",
            csrf_token="c",
            state="bogus",  # type: ignore[arg-type]
            created_at=now,
            last_seen_at=now,
            expires_at=now + TTL,
        )


def test_create_get_roundtrip(audit_log: Path) -> None:
    service = make_service(Clock())
    session, raw_token, csrf = service.create_session("user-1", TTL)
    assert raw_token is not None
    assert service.get_session(raw_token) == session
    assert session.csrf_token == csrf
    assert session.state == SessionState.ACTIVE
    assert audit_log.exists()


def test_idempotent_create_returns_same_session(audit_log: Path) -> None:
    clock = Clock()
    store = InMemorySessionStore()
    service = IdentityService(store, clock=clock)
    first, _, _ = service.create_session("user-1", TTL, idempotency_key="key-1")
    second, replayed_token, _ = service.create_session("user-1", TTL, idempotency_key="key-1")
    assert first == second
    assert replayed_token is None
    assert len(store.list_by_user(None)) == 1


def test_token_hash_not_reversible(audit_log: Path) -> None:
    service = make_service(Clock())
    session, raw_token, _ = service.create_session("user-1", TTL)
    assert raw_token is not None
    assert raw_token not in session.model_dump_json()
    assert session.token_hash == hash_token(raw_token)
    assert session.token_hash != raw_token


def test_rotation_invalidates_old_token_and_links_successor(audit_log: Path) -> None:
    service = make_service(Clock())
    _, raw_token, _ = service.create_session("user-1", TTL)
    successor, new_token, _ = service.rotate_session(raw_token)
    assert service.get_session(raw_token) is None
    assert service.get_session(new_token) == successor
    assert successor.rotated_from is not None
    assert successor.id != successor.rotated_from


def test_revoke_is_idempotent(audit_log: Path) -> None:
    service = make_service(Clock())
    _, raw_token, _ = service.create_session("user-1", TTL)
    revoked = service.revoke_session(raw_token)
    assert revoked is not None
    assert revoked.state == SessionState.REVOKED
    assert service.revoke_session(raw_token) == revoked
    before = len(audit.read_events())
    service.revoke_session(raw_token)
    assert len(audit.read_events()) == before
    assert service.get_session(raw_token) is None


def test_expiry_via_injected_clock(audit_log: Path) -> None:
    clock = Clock()
    service = make_service(clock)
    _, raw_token, _ = service.create_session("user-1", TTL)
    assert service.get_session(raw_token) is not None
    clock.advance(TTL + timedelta(seconds=1))
    assert service.get_session(raw_token) is None


def test_touch_updates_last_seen(audit_log: Path) -> None:
    clock = Clock()
    service = make_service(clock)
    session, raw_token, _ = service.create_session("user-1", TTL)
    clock.advance(timedelta(seconds=5))
    touched = service.touch(raw_token)
    assert touched is not None
    assert touched.last_seen_at > session.last_seen_at


def test_csrf_tokens_unique_across_100_generations() -> None:
    tokens = {new_csrf_token() for _ in range(100)}
    assert len(tokens) == 100


def test_cookie_attributes() -> None:
    attributes = cookie_attributes(secure=True)
    assert attributes["httpOnly"] is True
    assert attributes["secure"] is True
    assert attributes["sameSite"] == "Lax"


def test_audit_events_recorded(audit_log: Path) -> None:
    service = make_service(Clock())
    _, raw_token, _ = service.create_session("user-1", TTL)
    _, next_raw, _ = service.rotate_session(raw_token)
    service.revoke_session(next_raw)
    actions = [event["action"] for event in audit.read_events()]
    assert actions == ["session.created", "session.rotated", "session.revoked"]
    for event in audit.read_events():
        assert "raw_token" not in str(event["details"])


def test_concurrent_create_distinct_and_same_keys() -> None:
    clock = Clock()
    store = InMemorySessionStore()
    service = IdentityService(store, clock=clock)

    def create(key: str) -> Session:
        session, _, _ = service.create_session("user-1", TTL, idempotency_key=key)
        return session

    distinct = []
    threads = [
        threading.Thread(target=lambda k=f"key-{i}": distinct.append(create(k))) for i in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({session.id for session in distinct}) == 8
    assert len(store.list_by_user(None)) == 8

    same: list[Session] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def create_same() -> None:
        barrier.wait()
        session, _, _ = service.create_session("user-1", TTL, idempotency_key="shared")
        with lock:
            same.append(session)

    threads = [threading.Thread(target=create_same) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({session.id for session in same}) == 1
    assert len(store.list_by_user(None)) == 9
