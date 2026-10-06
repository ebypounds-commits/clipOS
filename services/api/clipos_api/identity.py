"""Identity/session foundation: typed contracts, store protocol, session service.

PostgreSQL persistence is deferred to CLIP-010; this module ships the store
abstraction plus a thread-safe in-memory implementation. Raw bearer tokens are
never stored: only their SHA-256 hashes are kept, and raw tokens are returned to
callers exactly once.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from clipos_api import audit

_EMAIL_MIN_PARTS = 2


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class SessionState(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


def _validate_email(value: str) -> str:
    parts = value.split("@")
    if len(parts) != _EMAIL_MIN_PARTS or not parts[0] or not parts[1]:
        raise ValueError("email must be a non-empty local@domain address")
    return value


class User(BaseModel):
    """Identity user contract."""

    model_config = ConfigDict(frozen=True)

    id: str
    email: str
    subject: str
    display_name: str
    status: UserStatus = UserStatus.ACTIVE
    created_at: datetime
    updated_at: datetime
    org_ref: str | None = None

    @field_validator("email")
    @classmethod
    def _email_valid(cls, value: str) -> str:
        return _validate_email(value)

    @field_validator("id", "subject")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value:
            raise ValueError("value must not be empty")
        return value


class Session(BaseModel):
    """Identity session contract. Stores token hashes only, never raw tokens."""

    model_config = ConfigDict(frozen=True)

    id: str
    user_id: str
    token_hash: str
    csrf_token: str
    state: SessionState = SessionState.ACTIVE
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    rotated_from: str | None = None
    idempotency_key: str | None = None

    @field_validator("id", "user_id", "token_hash", "csrf_token")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value:
            raise ValueError("value must not be empty")
        return value

    @field_validator("expires_at", "created_at", "last_seen_at")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware")
        return value


@runtime_checkable
class SessionStore(Protocol):
    """Persistence boundary for sessions; CLIP-010 provides the SQL store."""

    def get(self, session_id: str) -> Session | None:
        """Return the session with the given id, or None."""
        ...

    def put_if_absent(self, session: Session) -> Session:
        """Insert the session; return the existing one on idempotency-key collision."""
        ...

    def update(self, session: Session) -> None:
        """Replace the stored session by id."""
        ...

    def delete(self, session_id: str) -> None:
        """Remove the session with the given id, if present."""
        ...

    def list_by_user(self, user_id: str | None = None) -> list[Session]:
        """List sessions for a user, or all sessions when user_id is None."""
        ...


class InMemorySessionStore:
    """Thread-safe in-memory SessionStore keyed by id and idempotency key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._idempotency: dict[str, str] = {}

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def put_if_absent(self, session: Session) -> Session:
        with self._lock:
            if session.idempotency_key is not None:
                existing_id = self._idempotency.get(session.idempotency_key)
                if existing_id is not None:
                    return self._sessions[existing_id]
            self._sessions[session.id] = session
            if session.idempotency_key is not None:
                self._idempotency[session.idempotency_key] = session.id
            return session

    def update(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.id] = session

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def list_by_user(self, user_id: str | None = None) -> list[Session]:
        with self._lock:
            sessions = list(self._sessions.values())
        if user_id is None:
            return sessions
        return [session for session in sessions if session.user_id == user_id]


def hash_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest of a raw bearer token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    """Generate a fresh cryptographically random CSRF token."""
    return secrets.token_urlsafe(32)


def cookie_attributes(secure: bool = True) -> dict[str, Any]:
    """Return the secure session-cookie attribute mapping required by 07."""
    return {"httpOnly": True, "secure": secure, "sameSite": "Lax"}


class IdentityService:
    """Session lifecycle service: create, rotate, revoke, touch, resolve."""

    def __init__(
        self,
        store: SessionStore,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._clock = clock

    def _resolve(self, raw_token: str) -> Session | None:
        token_hash = hash_token(raw_token)
        for session in self._store.list_by_user(None):
            if session.token_hash == token_hash:
                return session
        return None

    def _is_expired(self, session: Session, now: datetime) -> bool:
        return session.state == SessionState.ACTIVE and session.expires_at <= now

    def create_session(
        self,
        user_id: str,
        ttl: timedelta,
        idempotency_key: str | None = None,
    ) -> tuple[Session, str | None, str]:
        """Create a session; with an idempotency key, replay returns the same session.

        Raw tokens are returned once (None on replay); the store keeps hashes only.
        """
        now = self._clock()
        session_id = secrets.token_urlsafe(16)
        raw_token = secrets.token_urlsafe(32)
        csrf_token = new_csrf_token()
        session = Session(
            id=session_id,
            user_id=user_id,
            token_hash=hash_token(raw_token),
            csrf_token=csrf_token,
            created_at=now,
            last_seen_at=now,
            expires_at=now + ttl,
            idempotency_key=idempotency_key,
        )
        stored = self._store.put_if_absent(session)
        if stored.id == session.id:
            audit.record(
                "session.created",
                "identity-service",
                stored.id,
                {"user_id": stored.user_id},
            )
            return stored, raw_token, stored.csrf_token
        return stored, None, stored.csrf_token

    def rotate_session(self, raw_token: str) -> tuple[Session, str, str]:
        """Validate the token, revoke the old session, and create its successor."""
        now = self._clock()
        old = self._resolve(raw_token)
        if old is None or old.state != SessionState.ACTIVE or self._is_expired(old, now):
            raise ValueError("invalid or expired session token")
        next_raw = secrets.token_urlsafe(32)
        successor = Session(
            id=secrets.token_urlsafe(16),
            user_id=old.user_id,
            token_hash=hash_token(next_raw),
            csrf_token=new_csrf_token(),
            created_at=now,
            last_seen_at=now,
            expires_at=old.expires_at,
            rotated_from=old.id,
        )
        revoked_old = old.model_copy(update={"state": SessionState.REVOKED})
        self._store.update(revoked_old)
        self._store.put_if_absent(successor)
        audit.record(
            "session.rotated",
            "identity-service",
            successor.id,
            {"user_id": successor.user_id, "rotated_from": old.id},
        )
        return successor, next_raw, successor.csrf_token

    def revoke_session(self, raw_token: str) -> Session | None:
        """Revoke the session for the token; already-revoked sessions are no-ops."""
        session = self._resolve(raw_token)
        if session is None or session.state == SessionState.REVOKED:
            return session
        revoked = session.model_copy(update={"state": SessionState.REVOKED})
        self._store.update(revoked)
        audit.record(
            "session.revoked",
            "identity-service",
            revoked.id,
            {"user_id": revoked.user_id},
        )
        return revoked

    def touch(self, raw_token: str) -> Session | None:
        """Update last_seen_at for the active, unexpired session."""
        now = self._clock()
        session = self._resolve(raw_token)
        if (
            session is None
            or session.state != SessionState.ACTIVE
            or self._is_expired(session, now)
        ):
            return None
        touched = session.model_copy(update={"last_seen_at": now})
        self._store.update(touched)
        return touched

    def get_session(self, raw_token: str) -> Session | None:
        """Return the session for the token, or None when unknown/expired/revoked."""
        now = self._clock()
        session = self._resolve(raw_token)
        if session is None or session.state != SessionState.ACTIVE:
            return None
        if self._is_expired(session, now):
            return None
        return session
