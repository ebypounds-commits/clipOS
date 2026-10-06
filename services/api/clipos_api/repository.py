"""PostgreSQL persistence foundation (M2): models, engine/session base, repositories.

The six foundation tables from ``03_DOMAIN_DATA_MODEL.md`` (users, organizations,
memberships, sessions, service_identities, audit_events) are declared with
SQLAlchemy 2.x declarative models and are portable across PostgreSQL and SQLite:
ids are strings, datetimes are timezone-aware ``DateTime`` columns, and enum-like
values are plain strings (no CHECK constraints), per the constrained-text guidance.

``BaseRepository`` is a generic record-level repository.  The concrete
``OrganizationRepository`` and ``MembershipRepository`` operate on mapped records,
while ``SQLOrganizationStore`` and ``SQLMembershipStore`` provide FULL conformance
with the store protocols in ``clipos_api.tenancy`` (contract-to-record mappers and
``put_if_absent`` semantics matching the in-memory stores) so that ``TenancyService``
can be constructed directly against the SQL stores.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    StaticPool,
    String,
    create_engine,
    make_url,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from clipos_api import tenancy


def _aware(value: datetime) -> datetime:
    """Normalize values read from backends that strip the timezone (SQLite)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class Base(DeclarativeBase):
    """Declarative base for all ClipOS API models."""


class UserRecord(Base):
    """Identity user row (``clipos_api.identity.User`` contract)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    org_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)


class OrganizationRecord(Base):
    """Tenant organization row (``clipos_api.tenancy.Organization`` contract)."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)


class MembershipRecord(Base):
    """Organization membership row (``clipos_api.tenancy.Membership`` contract)."""

    __tablename__ = "memberships"
    __table_args__ = (
        Index("uq_memberships_org_user", "org_id", "user_id", unique=True),
        Index("ix_memberships_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    joined_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)


class SessionRecord(Base):
    """Identity session row; token hashes only, never raw tokens."""

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        Index("uq_sessions_token_hash", "token_hash", unique=True),
        Index("uq_sessions_idempotency_key", "idempotency_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    rotated_from: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ServiceIdentityRecord(Base):
    """Machine identity row; credential references and token hashes only."""

    __tablename__ = "service_identities"
    __table_args__ = (Index("uq_service_identities_name", "name", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    disabled_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEventRecord(Base):
    """Hash-chained audit event row (``clipos_api.audit_trail.AuditEvent`` contract)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_timestamp", "timestamp"),
        Index("ix_audit_events_action", "action"),
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)


_engine_lock = threading.Lock()
_engines: dict[str, Engine] = {}


def get_engine(url: str) -> Engine:
    """Return a process-cached engine per URL.

    SQLite engines use a shared in-process connection pool so that the common
    ``sqlite:///:memory:`` URL works across sessions.
    """
    make_url(url)  # validates the URL early
    with _engine_lock:
        engine = _engines.get(url)
        if engine is None:
            kwargs: dict[str, Any] = {"pool_pre_ping": True}
            if make_url(url).get_backend_name() == "sqlite":
                kwargs["poolclass"] = StaticPool
                kwargs["connect_args"] = {"check_same_thread": False}
            engine = create_engine(url, **kwargs)
            _engines[url] = engine
        return engine


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Run work in one transaction: commit on success, roll back on error, always close."""
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class BaseRepository[ModelT: Base]:
    """Generic record repository with a session-per-operation pattern."""

    model: type[ModelT]

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def add(self, record: ModelT) -> ModelT:
        """Insert the record; the caller's instance is returned."""
        with session_scope(self._engine) as session:
            session.add(record)
            session.flush()
            return record

    def get(self, record_id: str) -> ModelT | None:
        """Return the record with the given primary key, or None."""
        with session_scope(self._engine) as session:
            return session.get(self.model, record_id)

    def list(self, limit: int = 50, offset: int = 0) -> list[ModelT]:
        """List records with limit/offset paging (no implicit ordering)."""
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        with session_scope(self._engine) as session:
            return list(session.scalars(select(self.model).limit(limit).offset(offset)))

    def delete(self, record_id: str) -> bool:
        """Delete the record by primary key; return True when a row was removed."""
        with session_scope(self._engine) as session:
            record = session.get(self.model, record_id)
            if record is None:
                return False
            session.delete(record)
            return True


class OrganizationRepository(BaseRepository[OrganizationRecord]):
    """Record-level repository for organizations."""

    model = OrganizationRecord


class MembershipRepository(BaseRepository[MembershipRecord]):
    """Record-level repository for memberships."""

    model = MembershipRecord


def _organization_record(org: tenancy.Organization) -> OrganizationRecord:
    return OrganizationRecord(
        id=org.id,
        name=org.name,
        slug=org.slug,
        state=org.state.value,
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


def _organization_contract(record: OrganizationRecord) -> tenancy.Organization:
    return tenancy.Organization(
        id=record.id,
        name=record.name,
        slug=record.slug,
        state=tenancy.OrgState(record.state),
        created_at=_aware(record.created_at),
        updated_at=_aware(record.updated_at),
    )


def _membership_record(membership: tenancy.Membership) -> MembershipRecord:
    return MembershipRecord(
        id=membership.id,
        org_id=membership.org_id,
        user_id=membership.user_id,
        role=membership.role.value,
        state=membership.state.value,
        joined_at=membership.joined_at,
        updated_at=membership.updated_at,
    )


def _membership_contract(record: MembershipRecord) -> tenancy.Membership:
    return tenancy.Membership(
        id=record.id,
        org_id=record.org_id,
        user_id=record.user_id,
        role=tenancy.MembershipRole(record.role),
        state=tenancy.MembershipState(record.state),
        joined_at=_aware(record.joined_at),
        updated_at=_aware(record.updated_at),
    )


class SQLOrganizationStore:
    """SQL-backed ``tenancy.OrganizationStore`` (full Protocol conformance).

    Protocol methods map to rows through the organization mappers; slug
    collisions surface through the unique slug index.  ``update`` mirrors the
    in-memory store's replace-by-id semantics.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, org_id: str) -> tenancy.Organization | None:
        with session_scope(self._engine) as session:
            record = session.get(OrganizationRecord, org_id)
            return _organization_contract(record) if record is not None else None

    def get_by_slug(self, slug: str) -> tenancy.Organization | None:
        with session_scope(self._engine) as session:
            record = self._by_slug(session, slug)
            return _organization_contract(record) if record is not None else None

    @staticmethod
    def _by_slug(session: Session, slug: str) -> OrganizationRecord | None:
        return session.scalars(
            select(OrganizationRecord).where(OrganizationRecord.slug == slug)
        ).first()

    def put_if_absent_by_slug(self, org: tenancy.Organization) -> tenancy.Organization:
        """Insert the organization; return the existing row on slug collision."""
        with session_scope(self._engine) as session:
            existing = self._by_slug(session, org.slug)
            if existing is not None:
                return _organization_contract(existing)
            record = _organization_record(org)
            try:
                session.add(record)
                session.flush()
            except IntegrityError:
                session.rollback()
                existing = self._by_slug(session, org.slug)
                if existing is None:
                    raise
                return _organization_contract(existing)
            return _organization_contract(record)

    def update(self, org: tenancy.Organization) -> None:
        """Replace the stored organization by id (creates it if absent)."""
        with session_scope(self._engine) as session:
            record = session.get(OrganizationRecord, org.id)
            if record is None:
                session.add(_organization_record(org))
                return
            record.name = org.name
            record.slug = org.slug
            record.state = org.state.value
            record.created_at = org.created_at
            record.updated_at = org.updated_at


class SQLMembershipStore:
    """SQL-backed ``tenancy.MembershipStore`` (full Protocol conformance).

    Protocol methods map to rows through the membership mappers; the unique
    ``(org_id, user_id)`` index backs ``put_if_absent``.  ``update`` mirrors the
    in-memory store's replace-by-key semantics.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, org_id: str, user_id: str) -> tenancy.Membership | None:
        with session_scope(self._engine) as session:
            record = self._by_key(session, org_id, user_id)
            return _membership_contract(record) if record is not None else None

    @staticmethod
    def _by_key(session: Session, org_id: str, user_id: str) -> MembershipRecord | None:
        return session.scalars(
            select(MembershipRecord)
            .where(MembershipRecord.org_id == org_id)
            .where(MembershipRecord.user_id == user_id)
        ).first()

    def put_if_absent(self, membership: tenancy.Membership) -> tenancy.Membership:
        """Insert the membership; return the existing row on key collision."""
        with session_scope(self._engine) as session:
            existing = self._by_key(session, membership.org_id, membership.user_id)
            if existing is not None:
                return _membership_contract(existing)
            record = _membership_record(membership)
            try:
                session.add(record)
                session.flush()
            except IntegrityError:
                session.rollback()
                existing = self._by_key(session, membership.org_id, membership.user_id)
                if existing is None:
                    raise
                return _membership_contract(existing)
            return _membership_contract(record)

    def update(self, membership: tenancy.Membership) -> None:
        """Replace the stored membership by (org_id, user_id) (creates if absent)."""
        with session_scope(self._engine) as session:
            record = self._by_key(session, membership.org_id, membership.user_id)
            if record is None:
                session.add(_membership_record(membership))
                return
            record.role = membership.role.value
            record.state = membership.state.value
            record.joined_at = membership.joined_at
            record.updated_at = membership.updated_at

    def list_by_org(self, org_id: str) -> list[tenancy.Membership]:
        with session_scope(self._engine) as session:
            records = session.scalars(
                select(MembershipRecord).where(MembershipRecord.org_id == org_id)
            ).all()
            return [_membership_contract(record) for record in records]

    def list_by_user(self, user_id: str) -> list[tenancy.Membership]:
        with session_scope(self._engine) as session:
            records = session.scalars(
                select(MembershipRecord).where(MembershipRecord.user_id == user_id)
            ).all()
            return [_membership_contract(record) for record in records]
