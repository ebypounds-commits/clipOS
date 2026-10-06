"""Service identity foundation: typed contracts, store protocol, identity service.

Service identities are machine principals (Service Agents, integrations, workers)
with least-privilege RBAC wired through CLIP-008's ``service_agent`` built-in
role.  Per 03 and 07, credentials are stored as REFERENCES only (e.g.
``vault:prod/<name>``) — raw secrets never enter this module.  Service tokens
follow the CLIP-006 identity pattern: raw tokens are returned to callers exactly
once and only their SHA-256 hashes are stored.

PostgreSQL persistence is deferred to CLIP-010; this module ships the store
abstraction plus a thread-safe in-memory implementation.
"""

from __future__ import annotations

import re
import secrets
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from clipos_api import audit
from clipos_api.identity import hash_token
from clipos_api.tenancy import validate_slug


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ServiceIdentityKind(StrEnum):
    SERVICE_AGENT = "service_agent"
    INTEGRATION = "integration"
    WORKER = "worker"


class ServiceIdentityState(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


_CREDENTIAL_REF_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*:\S+$")


class ServiceIdentity(BaseModel):
    """A machine identity with credential references and a token hash only."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    org_id: str | None = None
    kind: ServiceIdentityKind
    state: ServiceIdentityState = ServiceIdentityState.ACTIVE
    credential_ref: str | None = None
    token_hash: str | None = None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def _slug_valid(cls, value: str) -> str:
        return validate_slug(value)

    @field_validator("id")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value:
            raise ValueError("id must not be empty")
        return value

    @field_validator("credential_ref")
    @classmethod
    def _reference_only(cls, value: str | None) -> str | None:
        """Validate that the value looks like a reference, never a raw secret."""
        if value is None:
            return None
        if _CREDENTIAL_REF_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "credential_ref must be a reference like 'vault:prod/<name>', not a raw secret"
            )
        return value

    @field_validator("token_hash")
    @classmethod
    def _hash_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("token_hash must be a SHA-256 hex digest (64 hex chars)")
        return value

    @field_validator("created_at", "updated_at", "disabled_at")
    @classmethod
    def _timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware")
        return value


@runtime_checkable
class ServiceIdentityStore(Protocol):
    """Persistence boundary for service identities; CLIP-010 provides the SQL store."""

    def get(self, name: str) -> ServiceIdentity | None:
        """Return the identity with the given name, or None."""
        ...

    def put_if_absent(self, identity: ServiceIdentity) -> ServiceIdentity:
        """Insert the identity; return the existing one on name collision."""
        ...

    def update(self, identity: ServiceIdentity) -> None:
        """Replace the stored identity by name."""
        ...

    def list_all(self) -> list[ServiceIdentity]:
        """List all stored identities."""
        ...


class InMemoryServiceIdentityStore:
    """Thread-safe in-memory ServiceIdentityStore keyed uniquely by name."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._identities: dict[str, ServiceIdentity] = {}

    def get(self, name: str) -> ServiceIdentity | None:
        with self._lock:
            return self._identities.get(name)

    def put_if_absent(self, identity: ServiceIdentity) -> ServiceIdentity:
        with self._lock:
            existing = self._identities.get(identity.name)
            if existing is not None:
                return existing
            self._identities[identity.name] = identity
            return identity

    def update(self, identity: ServiceIdentity) -> None:
        with self._lock:
            self._identities[identity.name] = identity

    def list_all(self) -> list[ServiceIdentity]:
        with self._lock:
            return list(self._identities.values())


class ServiceIdentityService:
    """Service identity lifecycle: create, rotate, disable, enable, authenticate."""

    def __init__(
        self,
        store: ServiceIdentityStore | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store or InMemoryServiceIdentityStore()
        self._clock = clock
        self._lock = threading.RLock()

    def create(
        self,
        name: str,
        kind: ServiceIdentityKind,
        org_id: str | None = None,
        credential_ref: str | None = None,
    ) -> tuple[ServiceIdentity, str | None]:
        """Create the identity for ``name``; replays return the existing identity.

        A raw token is returned ONCE on first creation (None on replay); the
        store keeps only its SHA-256 hash.  ``service_identity.created`` is
        audited only on the material first transition.
        """
        now = self._clock()
        identity = ServiceIdentity(
            id=str(uuid.uuid4()),
            name=name,
            org_id=org_id,
            kind=kind,
            credential_ref=credential_ref,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            stored = self._store.put_if_absent(identity)
            if stored.id == identity.id:
                raw_token = secrets.token_urlsafe(32)
                updated = stored.model_copy(update={"token_hash": hash_token(raw_token)})
                self._store.update(updated)
                audit.record(
                    "service_identity.created",
                    "service-identity-service",
                    stored.name,
                    {"kind": stored.kind.value, "org_id": stored.org_id},
                )
                return updated, raw_token
            return stored, None

    def issue_token(self, name: str) -> tuple[ServiceIdentity, str]:
        """Rotate the identity's token; the old token becomes invalid immediately."""
        raw_token = secrets.token_urlsafe(32)
        with self._lock:
            identity = self._store.get(name)
            if identity is None:
                raise ValueError(f"service identity not found: {name}")
            updated = identity.model_copy(
                update={"token_hash": hash_token(raw_token), "updated_at": self._clock()}
            )
            self._store.update(updated)
            audit.record(
                "service_identity.token_rotated",
                "service-identity-service",
                name,
                {"kind": updated.kind.value},
            )
            return updated, raw_token

    def disable(self, name: str) -> ServiceIdentity:
        """Mark the identity disabled (idempotent); disabled identities cannot authenticate."""
        with self._lock:
            identity = self._store.get(name)
            if identity is None:
                raise ValueError(f"service identity not found: {name}")
            if identity.state == ServiceIdentityState.DISABLED:
                return identity
            now = self._clock()
            updated = identity.model_copy(
                update={
                    "state": ServiceIdentityState.DISABLED,
                    "disabled_at": now,
                    "updated_at": now,
                }
            )
            self._store.update(updated)
            audit.record(
                "service_identity.disabled",
                "service-identity-service",
                name,
                {"kind": updated.kind.value},
            )
            return updated

    def enable(self, name: str) -> ServiceIdentity:
        """Reactivate a disabled identity (idempotent)."""
        with self._lock:
            identity = self._store.get(name)
            if identity is None:
                raise ValueError(f"service identity not found: {name}")
            if identity.state == ServiceIdentityState.ACTIVE:
                return identity
            now = self._clock()
            updated = identity.model_copy(
                update={
                    "state": ServiceIdentityState.ACTIVE,
                    "disabled_at": None,
                    "updated_at": now,
                }
            )
            self._store.update(updated)
            audit.record(
                "service_identity.enabled",
                "service-identity-service",
                name,
                {"kind": updated.kind.value},
            )
            return updated

    def authenticate(self, raw_token: str) -> ServiceIdentity | None:
        """Return the active identity for ``raw_token``, or None when unknown/disabled/rotated."""
        token_hash = hash_token(raw_token)
        with self._lock:
            for identity in self._store.list_all():
                if identity.token_hash == token_hash:
                    if identity.state != ServiceIdentityState.ACTIVE:
                        return None
                    return identity
        return None

    def get(self, name: str) -> ServiceIdentity | None:
        return self._store.get(name)

    def list(self, org_id: str | None = None) -> list[ServiceIdentity]:
        identities = self._store.list_all()
        if org_id is None:
            return identities
        return [i for i in identities if i.org_id == org_id]
