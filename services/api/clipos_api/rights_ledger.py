"""Rights ledger: records, licenses, evidence, attribution, status events.

Rights records are org-scoped and idempotent per ``(item_id, org_id)``.
Initial status is never ``CLEAR``; status transitions are append-only events
and are audited with references only.  Evidence is append-only and attribution
requirements are idempotent per ``(record_id, applies_to, requirement)``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base

_DEFAULT_ACTOR = "registry-operator"


class Status(str, Enum):
    """Spec-constrained rights-record status values."""

    UNKNOWN = "UNKNOWN"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CLEAR = "CLEAR"
    RESTRICTED = "RESTRICTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    REJECTED = "REJECTED"


class Basis(str, Enum):
    """Spec-constrained rights-record basis values."""

    OWNED = "OWNED"
    LICENSED = "LICENSED"
    CREATOR_PARTNERSHIP = "CREATOR_PARTNERSHIP"
    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"
    PERMISSIVE_LICENSE = "PERMISSIVE_LICENSE"
    GENERATED = "GENERATED"
    TRANSFORMATIVE_REVIEW = "TRANSFORMATIVE_REVIEW"


class EvidenceType(str, Enum):
    """Accepted evidence attachment types."""

    CONTRACT = "contract"
    INVOICE = "invoice"
    EMAIL = "email"
    ATTESTATION = "attestation"


class AppliesTo(str, Enum):
    """Publication targets an attribution requirement can apply to."""

    PUBLICATION = "publication"
    DERIVATIVE = "derivative"
    EXCERPT = "excerpt"


class StatusChangeRequest(BaseModel):
    """Validated status transition payload."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    to_status: Status
    reason_ref: str | None = Field(default=None, max_length=512)


class RightsRecord(BaseModel):
    """One rights assessment for one source item."""

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1, max_length=128)
    item_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    status: Status
    basis: Basis
    reviewed_by: str = Field(min_length=1, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    @field_validator("status")
    @classmethod
    def _initial_must_not_clear(cls, value: Status) -> Status:
        if value not in (Status.UNKNOWN, Status.REVIEW_REQUIRED):
            raise ValueError("initial status must be UNKNOWN or REVIEW_REQUIRED")
        return value


class License(BaseModel):
    """One license attached to a rights record."""

    model_config = ConfigDict(frozen=True)

    license_id: str = Field(min_length=1, max_length=128)
    record_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    licensor_ref: str = Field(min_length=1, max_length=512)
    licensee_ref: str = Field(min_length=1, max_length=512)
    starts_at: datetime
    ends_at: datetime | None = None
    terms_ref: str = Field(min_length=1, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class LicenseEvidence(BaseModel):
    """One append-only evidence attachment for a license."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1, max_length=128)
    license_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    evidence_type: EvidenceType
    evidence_ref: str = Field(min_length=1, max_length=512)
    captured_at: datetime
    created_at: datetime | None = None


class AttributionRequirement(BaseModel):
    """One attribution requirement attached to a rights record."""

    model_config = ConfigDict(frozen=True)

    requirement_id: str = Field(min_length=1, max_length=128)
    record_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    requirement: str = Field(min_length=1, max_length=1024)
    applies_to: AppliesTo
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class RightsEvent(BaseModel):
    """One append-only rights status transition."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=128)
    record_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    from_status: Status
    to_status: Status
    reason_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = None


class RightsRecordRecord(Base):
    """SQL row for one rights record."""

    __tablename__ = "rights_records"
    __table_args__ = (
        sa.Index("uq_rights_records_record_id", "record_id", unique=True),
        sa.Index(
            "uq_rights_records_item_org",
            "item_id",
            "org_id",
            unique=True,
        ),
        sa.Index("ix_rights_records_org_created", "org_id", "created_at"),
        sa.Index("ix_rights_records_item_status", "item_id", "status"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    basis: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    reviewed_by: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    record_metadata: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class LicenseRecord(Base):
    """SQL row for one registered license."""

    __tablename__ = "licenses"
    __table_args__ = (
        sa.Index("uq_licenses_license_id", "license_id", unique=True),
        sa.Index(
            "uq_licenses_record_terms_start",
            "record_id",
            "terms_ref",
            "starts_at",
            unique=True,
        ),
        sa.Index("ix_licenses_org_created", "org_id", "created_at"),
        sa.Index("ix_licenses_record", "record_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    license_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    record_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    licensor_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    licensee_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    starts_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    ends_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    terms_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    license_metadata: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class LicenseEvidenceRecord(Base):
    """SQL row for one append-only license evidence attachment."""

    __tablename__ = "license_evidence"
    __table_args__ = (
        sa.Index("uq_license_evidence_evidence_id", "evidence_id", unique=True),
        sa.Index("ix_license_evidence_org_captured", "org_id", "captured_at"),
        sa.Index("ix_license_evidence_license", "license_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    evidence_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    license_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    evidence_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    captured_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class AttributionRequirementRecord(Base):
    """SQL row for one attribution requirement."""

    __tablename__ = "attribution_requirements"
    __table_args__ = (
        sa.Index("uq_attribution_requirements_id", "requirement_id", unique=True),
        sa.Index(
            "uq_attribution_requirements_record_scope",
            "record_id",
            "applies_to",
            "requirement",
            unique=True,
        ),
        sa.Index("ix_attribution_requirements_org_created", "org_id", "created_at"),
        sa.Index("ix_attribution_requirements_record", "record_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    requirement_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    record_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    requirement: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    applies_to: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    requirement_metadata: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class RightsEventRecord(Base):
    """SQL row for one append-only rights status transition."""

    __tablename__ = "rights_events"
    __table_args__ = (
        sa.Index("uq_rights_events_event_id", "event_id", unique=True),
        sa.Index("ix_rights_events_org_created", "org_id", "created_at"),
        sa.Index("ix_rights_events_record", "record_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    record_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    from_status: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    reason_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    """Normalize values read from backends that strip the timezone (SQLite)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _record_from_row(row: RightsRecordRecord) -> RightsRecord:
    return RightsRecord(
        record_id=row.record_id,
        item_id=row.item_id,
        org_id=row.org_id,
        status=Status(row.status),
        basis=Basis(row.basis),
        reviewed_by=row.reviewed_by,
        metadata=row.record_metadata,
        created_at=_aware(row.created_at),
    )


def _license_from_row(row: LicenseRecord) -> License:
    return License(
        license_id=row.license_id,
        record_id=row.record_id,
        org_id=row.org_id,
        licensor_ref=row.licensor_ref,
        licensee_ref=row.licensee_ref,
        starts_at=_aware(row.starts_at),
        ends_at=_aware(row.ends_at) if row.ends_at is not None else None,
        terms_ref=row.terms_ref,
        metadata=row.license_metadata,
        created_at=_aware(row.created_at),
    )


def _evidence_from_row(row: LicenseEvidenceRecord) -> LicenseEvidence:
    return LicenseEvidence(
        evidence_id=row.evidence_id,
        license_id=row.license_id,
        org_id=row.org_id,
        evidence_type=EvidenceType(row.evidence_type),
        evidence_ref=row.evidence_ref,
        captured_at=_aware(row.captured_at),
        created_at=_aware(row.created_at),
    )


def _requirement_from_row(row: AttributionRequirementRecord) -> AttributionRequirement:
    return AttributionRequirement(
        requirement_id=row.requirement_id,
        record_id=row.record_id,
        org_id=row.org_id,
        requirement=row.requirement,
        applies_to=AppliesTo(row.applies_to),
        metadata=row.requirement_metadata,
        created_at=_aware(row.created_at),
    )


def _event_from_row(row: RightsEventRecord) -> RightsEvent:
    return RightsEvent(
        event_id=row.event_id,
        record_id=row.record_id,
        org_id=row.org_id,
        from_status=Status(row.from_status),
        to_status=Status(row.to_status),
        reason_ref=row.reason_ref,
        created_at=_aware(row.created_at),
    )


def _audit(action: str, subject: str, details: dict[str, Any]) -> None:
    audit.record(action=action, actor=_DEFAULT_ACTOR, subject=subject, details=details)


class RightsLedgerStore:
    """Session-bound SQL persistence for the rights ledger."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> RightsLedgerStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    @staticmethod
    def _item(session: Session, item_id: str, org_id: str) -> Any | None:
        """Return the source item of *org_id* — never another org's item."""
        from clipos_api.rights_entities import SourceItemRecord

        return session.scalars(
            sa.select(SourceItemRecord).where(
                SourceItemRecord.item_id == item_id,
                SourceItemRecord.org_id == org_id,
            )
        ).first()

    @staticmethod
    def _record(session: Session, record_id: str) -> RightsRecordRecord | None:
        return session.scalars(
            sa.select(RightsRecordRecord).where(RightsRecordRecord.record_id == record_id)
        ).first()

    @staticmethod
    def _license(session: Session, license_id: str) -> LicenseRecord | None:
        return session.scalars(
            sa.select(LicenseRecord).where(LicenseRecord.license_id == license_id)
        ).first()

    def get_record(self, record_id: str) -> RightsRecord | None:
        """Return one rights record, or None when it is not registered."""
        with self._sessions() as session:
            row = self._record(session, record_id)
            return _record_from_row(row) if row is not None else None

    def list_records(self, org_id: str) -> list[RightsRecord]:
        """Return org-scoped rights records ordered by record_id."""
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(RightsRecordRecord)
                .where(RightsRecordRecord.org_id == org_id)
                .order_by(RightsRecordRecord.record_id)
            ).all()
            return [_record_from_row(row) for row in rows]

    def list_events(self, record_id: str, org_id: str) -> list[RightsEvent]:
        """Return org-scoped status events for one rights record."""
        with self._sessions() as session:
            row = self._record(session, record_id)
            if row is None or row.org_id != org_id:
                raise ValueError(f"rights record not found: {record_id}")
            events = session.scalars(
                sa.select(RightsEventRecord)
                .where(RightsEventRecord.record_id == record_id)
                .order_by(RightsEventRecord.created_at, RightsEventRecord.id)
            ).all()
            return [_event_from_row(event) for event in events]

    def register_record(self, record: RightsRecord) -> RightsRecord:
        """Register one rights record; a matching re-registration is a no-op."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            item = self._item(session, record.item_id, record.org_id)
            if item is None:
                raise ValueError(f"source item not found: {record.item_id}")
            row = self._record(session, record.record_id)
            if row is not None:
                stored = _record_from_row(row)
                if (
                    stored.item_id != record.item_id
                    or stored.org_id != record.org_id
                    or stored.basis != record.basis
                    or stored.reviewed_by != record.reviewed_by
                    or stored.metadata != record.metadata
                ):
                    raise ValueError(
                        f"rights record already registered with different fields: {record.record_id}"
                    )
                return stored
            row = RightsRecordRecord(
                record_id=record.record_id,
                item_id=record.item_id,
                org_id=record.org_id,
                status=record.status.value,
                basis=record.basis.value,
                reviewed_by=record.reviewed_by,
                record_metadata=record.metadata,
                created_at=now,
            )
            session.add(row)
            session.flush()
            _audit(
                "rights_ledger.record_registered",
                f"rights_record:{record.record_id}",
                {
                    "org_id": record.org_id,
                    "item_id": record.item_id,
                    "status": record.status.value,
                },
            )
            return _record_from_row(row)

    def record_status_change(
        self,
        record_id: str,
        org_id: str,
        to_status: Status,
        reason_ref: str | None = None,
    ) -> RightsEvent:
        """Append one validated status transition and audit it."""
        if to_status not in Status:
            raise ValueError(f"invalid status: {to_status}")
        with self._sessions.begin() as session:
            row = self._record(session, record_id)
            if row is None or row.org_id != org_id:
                raise ValueError(f"rights record not found: {record_id}")
            from_status = Status(row.status)
            row.status = to_status.value
            session.flush()
            event_count = session.scalar(
                sa.select(sa.func.count())
                .select_from(RightsEventRecord)
                .where(RightsEventRecord.record_id == record_id)
            )
            event_id = f"evt-{record_id}-{(event_count or 0) + 1}"
            event = RightsEventRecord(
                event_id=event_id,
                record_id=record_id,
                org_id=org_id,
                from_status=from_status.value,
                to_status=to_status.value,
                reason_ref=reason_ref,
                created_at=datetime.now(UTC),
            )
            session.add(event)
            session.flush()
            stored = _event_from_row(event)
        _audit(
            "rights_ledger.status_changed",
            f"rights_record:{record_id}",
            {
                "org_id": org_id,
                "from_status": from_status.value,
                "to_status": to_status.value,
                "reason_ref": reason_ref,
            },
        )
        return stored

    def register_license(self, license_model: License) -> License:
        """Register one license; a matching re-registration is a no-op."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            record = self._record(session, license_model.record_id)
            if record is None:
                raise ValueError(f"rights record not found: {license_model.record_id}")
            if record.org_id != license_model.org_id:
                raise ValueError("rights record belongs to a different organization")
            existing = session.scalars(
                sa.select(LicenseRecord).where(
                    LicenseRecord.record_id == license_model.record_id,
                    LicenseRecord.terms_ref == license_model.terms_ref,
                    LicenseRecord.starts_at == license_model.starts_at,
                )
            ).first()
            if existing is not None:
                stored = _license_from_row(existing)
                if (
                    stored.org_id != license_model.org_id
                    or stored.licensor_ref != license_model.licensor_ref
                    or stored.licensee_ref != license_model.licensee_ref
                    or stored.ends_at != license_model.ends_at
                    or stored.metadata != license_model.metadata
                ):
                    raise ValueError(
                        "license already registered with different fields: "
                        f"{license_model.license_id}"
                    )
                return stored
            row = LicenseRecord(
                license_id=license_model.license_id,
                record_id=license_model.record_id,
                org_id=license_model.org_id,
                licensor_ref=license_model.licensor_ref,
                licensee_ref=license_model.licensee_ref,
                starts_at=license_model.starts_at,
                ends_at=license_model.ends_at,
                terms_ref=license_model.terms_ref,
                license_metadata=license_model.metadata,
                created_at=now,
            )
            session.add(row)
            session.flush()
            _audit(
                "rights_ledger.license_registered",
                f"license:{license_model.license_id}",
                {
                    "org_id": license_model.org_id,
                    "record_id": license_model.record_id,
                    "terms_ref": license_model.terms_ref,
                },
            )
            return _license_from_row(row)

    def add_evidence(
        self,
        evidence: LicenseEvidence,
        update: bool = False,
    ) -> LicenseEvidence:
        """Append license evidence; updates are rejected by policy."""
        if update:
            raise ValueError("license evidence is append-only")
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            license_row = self._license(session, evidence.license_id)
            if license_row is None:
                raise ValueError(f"license not found: {evidence.license_id}")
            if license_row.org_id != evidence.org_id:
                raise ValueError("license belongs to a different organization")
            row = LicenseEvidenceRecord(
                evidence_id=evidence.evidence_id,
                license_id=evidence.license_id,
                org_id=evidence.org_id,
                evidence_type=evidence.evidence_type.value,
                evidence_ref=evidence.evidence_ref,
                captured_at=evidence.captured_at,
                created_at=now,
            )
            session.add(row)
            session.flush()
            _audit(
                "rights_ledger.evidence_added",
                f"license:{evidence.license_id}",
                {
                    "org_id": evidence.org_id,
                    "evidence_id": evidence.evidence_id,
                    "evidence_type": evidence.evidence_type.value,
                },
            )
            return _evidence_from_row(row)

    def add_attribution_requirement(
        self, requirement: AttributionRequirement
    ) -> AttributionRequirement:
        """Add one attribution requirement; a matching re-registration is a no-op."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            record = self._record(session, requirement.record_id)
            if record is None:
                raise ValueError(f"rights record not found: {requirement.record_id}")
            if record.org_id != requirement.org_id:
                raise ValueError("rights record belongs to a different organization")
            existing = session.scalars(
                sa.select(AttributionRequirementRecord).where(
                    AttributionRequirementRecord.record_id == requirement.record_id,
                    AttributionRequirementRecord.applies_to == requirement.applies_to.value,
                    AttributionRequirementRecord.requirement == requirement.requirement,
                )
            ).first()
            if existing is not None:
                stored = _requirement_from_row(existing)
                if stored.org_id != requirement.org_id or stored.metadata != requirement.metadata:
                    raise ValueError(
                        "attribution requirement already registered with different fields: "
                        f"{requirement.requirement_id}"
                    )
                return stored
            row = AttributionRequirementRecord(
                requirement_id=requirement.requirement_id,
                record_id=requirement.record_id,
                org_id=requirement.org_id,
                requirement=requirement.requirement,
                applies_to=requirement.applies_to.value,
                requirement_metadata=requirement.metadata,
                created_at=now,
            )
            session.add(row)
            session.flush()
            _audit(
                "rights_ledger.attribution_required",
                f"rights_record:{requirement.record_id}",
                {
                    "org_id": requirement.org_id,
                    "requirement_id": requirement.requirement_id,
                    "applies_to": requirement.applies_to.value,
                },
            )
            return _requirement_from_row(row)

    def effective_status(
        self,
        record_id: str,
        org_id: str,
        now: datetime | None = None,
    ) -> Status:
        """Derive the effective status from licenses and recorded transitions."""
        at = now or datetime.now(UTC)
        with self._sessions() as session:
            record = self._record(session, record_id)
            if record is None or record.org_id != org_id:
                raise ValueError(f"rights record not found: {record_id}")
            events = session.scalars(
                sa.select(RightsEventRecord)
                .where(RightsEventRecord.record_id == record_id)
                .order_by(RightsEventRecord.created_at, RightsEventRecord.id)
            ).all()
            if events and Status(events[-1].to_status) in (
                Status.REVOKED,
                Status.REJECTED,
            ):
                return Status(events[-1].to_status)
            licenses = session.scalars(
                sa.select(LicenseRecord).where(LicenseRecord.record_id == record_id)
            ).all()
            if any(
                license_row.ends_at is not None and _aware(license_row.ends_at) < at
                for license_row in licenses
            ):
                return Status.EXPIRED
            active_license = any(
                license_row.ends_at is None or _aware(license_row.ends_at) >= at
                for license_row in licenses
            )
            if active_license:
                return Status.CLEAR
            return Status(record.status)


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> RightsLedgerStore:
    return RightsLedgerStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def register_record_route(
    request: RightsRecord,
    store: Annotated[RightsLedgerStore, Depends(_store)],
) -> RightsRecord:
    """Register one rights record (idempotent on matching definition)."""
    try:
        return store.register_record(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def get_record_route(
    record_id: str,
    org_id: Annotated[str, Query(min_length=1)],
    store: Annotated[RightsLedgerStore, Depends(_store)],
) -> RightsRecord:
    """Return one rights record, or 404."""
    with store._sessions() as session:
        row = store._record(session, record_id)
        if row is None or row.org_id != org_id:
            raise HTTPException(status_code=404, detail=f"rights record not found: {record_id}")
        return _record_from_row(row)


def list_records_route(
    store: Annotated[RightsLedgerStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1)],
) -> list[RightsRecord]:
    """Return org-scoped rights records ordered by record_id."""
    with store._sessions() as session:
        rows = session.scalars(
            sa.select(RightsRecordRecord)
            .where(RightsRecordRecord.org_id == org_id)
            .order_by(RightsRecordRecord.record_id)
        ).all()
        return [_record_from_row(row) for row in rows]


def record_status_route(
    record_id: str,
    request: StatusChangeRequest,
    store: Annotated[RightsLedgerStore, Depends(_store)],
) -> RightsEvent:
    """Append one validated status transition."""
    try:
        return store.record_status_change(
            record_id, request.org_id, request.to_status, request.reason_ref
        )
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def register_license_route(
    request: License,
    store: Annotated[RightsLedgerStore, Depends(_store)],
) -> License:
    """Register one license (idempotent on (record_id, terms_ref, starts_at))."""
    try:
        return store.register_license(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def add_evidence_route(
    license_id: str,
    request: LicenseEvidence,
    store: Annotated[RightsLedgerStore, Depends(_store)],
) -> LicenseEvidence:
    """Append one license evidence attachment."""
    if request.license_id != license_id:
        raise HTTPException(status_code=409, detail="license_id does not match path")
    try:
        return store.add_evidence(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def add_requirement_route(
    record_id: str,
    request: AttributionRequirement,
    store: Annotated[RightsLedgerStore, Depends(_store)],
) -> AttributionRequirement:
    """Add one attribution requirement (idempotent on matching definition)."""
    if request.record_id != record_id:
        raise HTTPException(status_code=409, detail="record_id does not match path")
    try:
        return store.add_attribution_requirement(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def list_events_route(
    record_id: str,
    org_id: Annotated[str, Query(min_length=1)],
    store: Annotated[RightsLedgerStore, Depends(_store)],
) -> list[RightsEvent]:
    """Return org-scoped status events for one rights record."""
    with store._sessions() as session:
        row = store._record(session, record_id)
        if row is None or row.org_id != org_id:
            raise HTTPException(status_code=404, detail=f"rights record not found: {record_id}")
        events = session.scalars(
            sa.select(RightsEventRecord)
            .where(RightsEventRecord.record_id == record_id)
            .order_by(RightsEventRecord.created_at, RightsEventRecord.id)
        ).all()
        return [_event_from_row(event) for event in events]


def effective_status_route(
    record_id: str,
    org_id: Annotated[str, Query(min_length=1)],
    store: Annotated[RightsLedgerStore, Depends(_store)],
) -> dict[str, str]:
    """Return the derived effective status for one rights record."""
    try:
        return {"record_id": record_id, "status": store.effective_status(record_id, org_id).value}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


router = APIRouter(tags=["rights-ledger"])
router.add_api_route(
    "/rights-records", register_record_route, methods=["POST"], name="register_rights_record"
)
router.add_api_route(
    "/rights-records", list_records_route, methods=["GET"], name="list_rights_records"
)
router.add_api_route(
    "/rights-records/{record_id}", get_record_route, methods=["GET"], name="get_rights_record"
)
router.add_api_route(
    "/rights-records/{record_id}/status",
    record_status_route,
    methods=["POST"],
    name="change_rights_status",
)
router.add_api_route(
    "/rights-records/{record_id}/effective-status",
    effective_status_route,
    methods=["GET"],
    name="effective_rights_status",
)
router.add_api_route(
    "/rights-records/{record_id}/licenses",
    register_license_route,
    methods=["POST"],
    name="register_license",
)
router.add_api_route(
    "/licenses/{license_id}/evidence",
    add_evidence_route,
    methods=["POST"],
    name="add_license_evidence",
)
router.add_api_route(
    "/rights-records/{record_id}/attribution-requirements",
    add_requirement_route,
    methods=["POST"],
    name="add_attribution_requirement",
)
router.add_api_route(
    "/rights-records/{record_id}/events",
    list_events_route,
    methods=["GET"],
    name="list_rights_events",
)
