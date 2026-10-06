"""Organization tenancy foundation: typed contracts, store protocol, tenancy service.

PostgreSQL persistence is deferred to CLIP-010; this module ships the store
abstractions plus thread-safe in-memory implementations. Roles here are a simple
enum for tenancy management only — RBAC/capabilities are CLIP-008. All privileged
changes are recorded through the shared ``clipos_api.audit`` module.
"""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from clipos_api import audit

_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OrgState(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class MembershipRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class MembershipState(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"


def validate_slug(value: str) -> str:
    if not _SLUG_PATTERN.fullmatch(value):
        raise ValueError(
            "slug must match ^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$ (lowercase letters, digits, dashes; length 2-63; no leading/trailing dash)"
        )
    return value


class Organization(BaseModel):
    """Tenant organization contract."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    slug: str
    state: OrgState = OrgState.ACTIVE
    created_at: datetime
    updated_at: datetime

    @field_validator("slug")
    @classmethod
    def _slug_valid(cls, value: str) -> str:
        return validate_slug(value)

    @field_validator("id", "name")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value:
            raise ValueError("value must not be empty")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware")
        return value


class Membership(BaseModel):
    """Organization membership contract."""

    model_config = ConfigDict(frozen=True)

    id: str
    org_id: str
    user_id: str
    role: MembershipRole = MembershipRole.MEMBER
    state: MembershipState = MembershipState.ACTIVE
    joined_at: datetime
    updated_at: datetime

    @field_validator("id", "org_id", "user_id")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value:
            raise ValueError("value must not be empty")
        return value

    @field_validator("joined_at", "updated_at")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware")
        return value


@runtime_checkable
class OrganizationStore(Protocol):
    """Persistence boundary for organizations; CLIP-010 provides the SQL store."""

    def get(self, org_id: str) -> Organization | None:
        """Return the organization with the given id, or None."""
        ...

    def get_by_slug(self, slug: str) -> Organization | None:
        """Return the organization with the given slug, or None."""
        ...

    def put_if_absent_by_slug(self, org: Organization) -> Organization:
        """Insert the organization; return the existing one on slug collision."""
        ...

    def update(self, org: Organization) -> None:
        """Replace the stored organization by id."""
        ...


@runtime_checkable
class MembershipStore(Protocol):
    """Persistence boundary for memberships; CLIP-010 provides the SQL store."""

    def get(self, org_id: str, user_id: str) -> Membership | None:
        """Return the membership for (org_id, user_id), or None."""
        ...

    def put_if_absent(self, membership: Membership) -> Membership:
        """Insert the membership; return the existing one on (org_id, user_id) collision."""
        ...

    def update(self, membership: Membership) -> None:
        """Replace the stored membership by (org_id, user_id); history is kept by state."""
        ...

    def list_by_org(self, org_id: str) -> list[Membership]:
        """List memberships for the organization."""
        ...

    def list_by_user(self, user_id: str) -> list[Membership]:
        """List memberships for the user."""
        ...


class InMemoryOrganizationStore:
    """Thread-safe in-memory OrganizationStore keyed by id with unique slugs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._orgs: dict[str, Organization] = {}
        self._slugs: dict[str, str] = {}

    def get(self, org_id: str) -> Organization | None:
        with self._lock:
            return self._orgs.get(org_id)

    def get_by_slug(self, slug: str) -> Organization | None:
        with self._lock:
            org_id = self._slugs.get(slug)
            return self._orgs.get(org_id) if org_id else None

    def put_if_absent_by_slug(self, org: Organization) -> Organization:
        with self._lock:
            existing_id = self._slugs.get(org.slug)
            if existing_id is not None:
                return self._orgs[existing_id]
            self._orgs[org.id] = org
            self._slugs[org.slug] = org.id
            return org

    def update(self, org: Organization) -> None:
        with self._lock:
            self._orgs[org.id] = org


class InMemoryMembershipStore:
    """Thread-safe in-memory MembershipStore keyed uniquely by (org_id, user_id).

    Removed memberships keep their row with ``state=removed`` so that re-adding
    reactivates the same membership rather than creating a duplicate.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._memberships: dict[tuple[str, str], Membership] = {}

    def get(self, org_id: str, user_id: str) -> Membership | None:
        with self._lock:
            return self._memberships.get((org_id, user_id))

    def put_if_absent(self, membership: Membership) -> Membership:
        with self._lock:
            key = (membership.org_id, membership.user_id)
            existing = self._memberships.get(key)
            if existing is not None:
                return existing
            self._memberships[key] = membership
            return membership

    def update(self, membership: Membership) -> None:
        with self._lock:
            self._memberships[(membership.org_id, membership.user_id)] = membership

    def list_by_org(self, org_id: str) -> list[Membership]:
        with self._lock:
            return [m for (oid, _), m in self._memberships.items() if oid == org_id]

    def list_by_user(self, user_id: str) -> list[Membership]:
        with self._lock:
            return [m for (_, uid), m in self._memberships.items() if uid == user_id]


class TenancyService:
    """Organization/membership lifecycle with server-side tenant-scoping checks.

    Invariants:
    - Every organization always has at least one ACTIVE owner; removing or demoting
      the final active owner raises ``ValueError``.
    - ``create_organization`` and ``add_member`` are idempotent; audit records fire
      only on the material first transition.
    - ``require_membership(user_id, org_id)`` returns the ACTIVE membership or
      ``None`` — never raises for absent membership. The caller MUST treat ``None``
      as "no access" (deny by default); this is the server-side tenant-scoping
      primitive required by 07 (tenant scoping enforced server-side). It returns
      ``None`` for suspended organizations as well: memberships persist but do not
      grant access while the organization is suspended.
    """

    def __init__(
        self,
        org_store: OrganizationStore | None = None,
        membership_store: MembershipStore | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._orgs = org_store or InMemoryOrganizationStore()
        self._memberships = membership_store or InMemoryMembershipStore()
        self._clock = clock
        self._lock = threading.RLock()

    def create_organization(
        self,
        name: str,
        slug: str,
        idempotency: str | None = None,
    ) -> Organization:
        """Create the organization for ``slug``; replays return the existing org.

        ``idempotency`` is accepted for caller key propagation but the slug is the
        canonical idempotency key. ``org.created`` is audited only on first creation.
        """
        del idempotency
        validate_slug(slug)
        if not name:
            raise ValueError("name must not be empty")
        now = self._clock()
        org = Organization(
            id=str(uuid.uuid4()),
            name=name,
            slug=slug,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            stored = self._orgs.put_if_absent_by_slug(org)
            if stored.id == org.id:
                audit.record(
                    "org.created",
                    "tenancy-service",
                    stored.id,
                    {"slug": stored.slug, "name": stored.name},
                )
            return stored

    def get_organization(self, org_id: str) -> Organization | None:
        return self._orgs.get(org_id)

    def set_org_state(self, org_id: str, state: OrgState) -> Organization:
        with self._lock:
            org = self._orgs.get(org_id)
            if org is None:
                raise ValueError("organization not found")
            if org.state == state:
                return org
            updated = org.model_copy(update={"state": state, "updated_at": self._clock()})
            self._orgs.update(updated)
            action = "org.suspended" if state == OrgState.SUSPENDED else "org.reactivated"
            audit.record(
                action,
                "tenancy-service",
                org_id,
                {"slug": org.slug},
            )
            return updated

    def _active_owners(self, org_id: str) -> list[Membership]:
        return [
            m
            for m in self._memberships.list_by_org(org_id)
            if m.state == MembershipState.ACTIVE and m.role == MembershipRole.OWNER
        ]

    def _require_org(self, org_id: str) -> Organization:
        org = self._orgs.get(org_id)
        if org is None:
            raise ValueError("organization not found")
        return org

    def add_member(
        self,
        org_id: str,
        user_id: str,
        role: MembershipRole = MembershipRole.MEMBER,
    ) -> Membership:
        """Add or reactivate a member; already-active members return unchanged."""
        if not user_id:
            raise ValueError("user_id must not be empty")
        now = self._clock()
        with self._lock:
            self._require_org(org_id)
            existing = self._memberships.get(org_id, user_id)
            if existing is not None and existing.state == MembershipState.ACTIVE:
                return existing
            if existing is None:
                membership = Membership(
                    id=str(uuid.uuid4()),
                    org_id=org_id,
                    user_id=user_id,
                    role=role,
                    joined_at=now,
                    updated_at=now,
                )
                stored = self._memberships.put_if_absent(membership)
            else:
                stored = existing.model_copy(
                    update={"role": role, "state": MembershipState.ACTIVE, "updated_at": now}
                )
                self._memberships.update(stored)
            audit.record(
                "member.added",
                "tenancy-service",
                stored.id,
                {"org_id": org_id, "user_id": user_id, "role": stored.role},
            )
            return stored

    def set_member_role(
        self,
        org_id: str,
        user_id: str,
        role: MembershipRole,
    ) -> Membership:
        """Change an active member's role, protecting the final active owner."""
        with self._lock:
            self._require_org(org_id)
            membership = self._memberships.get(org_id, user_id)
            if membership is None or membership.state != MembershipState.ACTIVE:
                raise ValueError("active membership not found")
            if membership.role == role:
                return membership
            if (
                membership.role == MembershipRole.OWNER
                and role != MembershipRole.OWNER
                and len(self._active_owners(org_id)) <= 1
            ):
                raise ValueError("cannot demote the final active owner of an organization")
            updated = membership.model_copy(update={"role": role, "updated_at": self._clock()})
            self._memberships.update(updated)
            audit.record(
                "member.role_changed",
                "tenancy-service",
                membership.id,
                {"org_id": org_id, "user_id": user_id, "from": membership.role, "to": role},
            )
            return updated

    def remove_member(self, org_id: str, user_id: str) -> Membership:
        """Mark a membership removed (idempotent), protecting the final active owner."""
        with self._lock:
            self._require_org(org_id)
            membership = self._memberships.get(org_id, user_id)
            if membership is None or membership.state == MembershipState.REMOVED:
                if membership is not None:
                    return membership
                raise ValueError("membership not found")
            if membership.role == MembershipRole.OWNER and len(self._active_owners(org_id)) <= 1:
                raise ValueError("cannot remove the final active owner of an organization")
            updated = membership.model_copy(
                update={"state": MembershipState.REMOVED, "updated_at": self._clock()}
            )
            self._memberships.update(updated)
            audit.record(
                "member.removed",
                "tenancy-service",
                membership.id,
                {"org_id": org_id, "user_id": user_id},
            )
            return updated

    def members_of_org(
        self,
        org_id: str,
        active_only: bool = True,
    ) -> list[Membership]:
        members = self._memberships.list_by_org(org_id)
        if active_only:
            return [m for m in members if m.state == MembershipState.ACTIVE]
        return members

    def orgs_of_user(self, user_id: str) -> list[Organization]:
        orgs: list[Organization] = []
        for membership in self._memberships.list_by_user(user_id):
            if membership.state != MembershipState.ACTIVE:
                continue
            org = self._orgs.get(membership.org_id)
            if org is not None:
                orgs.append(org)
        return orgs

    def require_membership(self, user_id: str, org_id: str) -> Membership | None:
        """Return the active membership, or None when the user has no active access.

        None is returned for unknown users/orgs, removed memberships, and suspended
        organizations. Callers must deny on None; this check happens server-side.
        """
        org = self._orgs.get(org_id)
        if org is None or org.state != OrgState.ACTIVE:
            return None
        membership = self._memberships.get(org_id, user_id)
        if membership is None or membership.state != MembershipState.ACTIVE:
            return None
        return membership

    def slug_available(self, slug: str) -> bool:
        validate_slug(slug)
        return self._orgs.get_by_slug(slug) is None
