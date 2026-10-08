"""Rights & Provenance foundational entities: creators, sources, source items.

Creators register with a secrets *reference* (never inline contact data).
Content sources validate their creator and are constrained to approved source
types.  Source items are idempotent on ``(source_id, external_ref)``.  All
listing operations are organization-scoped and every material mutation is
audited.
"""

from __future__ import annotations

import re
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
_SECRETS_REF_PATTERN = re.compile(r"^[a-z][a-z0-9-]*://.+$")
_SOURCE_TYPES = ("upload", "feed", "partner_feed", "scrape_approved")


class DeactivateSourceRequest(BaseModel):
    """Org-scoped deactivation payload."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)


class SourceType(str, Enum):
    """Approved content-source types."""

    UPLOAD = "upload"
    FEED = "feed"
    PARTNER_FEED = "partner_feed"
    SCRAPE_APPROVED = "scrape_approved"


class Creator(BaseModel):
    """One rights-bearing creator, org-scoped."""

    model_config = ConfigDict(frozen=True)

    creator_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=255)
    contact_ref: str = Field(min_length=3, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    @field_validator("contact_ref")
    @classmethod
    def _contact_must_be_reference(cls, value: str) -> str:
        """Reject inline contact data; only secrets references are accepted."""
        if not _SECRETS_REF_PATTERN.match(value):
            raise ValueError("contact_ref must be a secrets reference (scheme://key)")
        return value


class ContentSource(BaseModel):
    """One provenance content source attached to a registered creator."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    creator_id: str = Field(min_length=1, max_length=128)
    source_type: SourceType
    canonical_url: str | None = Field(default=None, max_length=2048)
    active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    @property
    def source_type_value(self) -> str:
        """Return the constrained-text storage value for the source type."""
        return self.source_type.value


class SourceItem(BaseModel):
    """One captured source item, idempotent by (source_id, external_ref)."""

    model_config = ConfigDict(frozen=True)

    item_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=128)
    external_ref: str = Field(min_length=1, max_length=512)
    captured_at: datetime
    content_ref: str = Field(min_length=1, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class CreatorRecord(Base):
    """SQL row for one registered creator."""

    __tablename__ = "creators"
    __table_args__ = (
        sa.Index("uq_creators_creator_id", "creator_id", unique=True),
        sa.Index("ix_creators_org_created", "org_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    creator_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    contact_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    creator_metadata: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ContentSourceRecord(Base):
    """SQL row for one registered content source."""

    __tablename__ = "content_sources"
    __table_args__ = (
        sa.Index("uq_content_sources_source_id", "source_id", unique=True),
        sa.Index("ix_content_sources_org_created", "org_id", "created_at"),
        sa.Index("ix_content_sources_creator", "creator_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    creator_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(sa.String(2048), nullable=True)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class SourceItemRecord(Base):
    """SQL row for one captured source item."""

    __tablename__ = "source_items"
    __table_args__ = (
        sa.Index(
            "uq_source_items_source_external_ref",
            "source_id",
            "external_ref",
            unique=True,
        ),
        sa.Index("uq_source_items_item_id", "item_id", unique=True),
        sa.Index("ix_source_items_org_captured", "org_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    external_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    captured_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    content_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    item_metadata: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    """Normalize values read from backends that strip the timezone (SQLite)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _creator_from_row(row: CreatorRecord) -> Creator:
    return Creator(
        creator_id=row.creator_id,
        org_id=row.org_id,
        display_name=row.display_name,
        contact_ref=row.contact_ref,
        metadata=row.creator_metadata,
        created_at=_aware(row.created_at),
    )


def _source_from_row(row: ContentSourceRecord) -> ContentSource:
    return ContentSource(
        source_id=row.source_id,
        org_id=row.org_id,
        creator_id=row.creator_id,
        source_type=SourceType(row.source_type),
        canonical_url=row.canonical_url,
        active=row.active,
        metadata=row.source_metadata,
        created_at=_aware(row.created_at),
    )


def _item_from_row(row: SourceItemRecord) -> SourceItem:
    return SourceItem(
        item_id=row.item_id,
        org_id=row.org_id,
        source_id=row.source_id,
        external_ref=row.external_ref,
        captured_at=_aware(row.captured_at),
        content_ref=row.content_ref,
        metadata=row.item_metadata,
        created_at=_aware(row.created_at),
    )


def _audit(action: str, subject: str, details: dict[str, Any]) -> None:
    audit.record(action=action, actor=_DEFAULT_ACTOR, subject=subject, details=details)


class RightsEntitiesStore:
    """Session-bound SQL persistence for rights-provenance entities."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> RightsEntitiesStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    @staticmethod
    def _creator(session: Session, creator_id: str) -> CreatorRecord | None:
        return session.scalars(
            sa.select(CreatorRecord).where(CreatorRecord.creator_id == creator_id)
        ).first()

    @staticmethod
    def _source(session: Session, source_id: str) -> ContentSourceRecord | None:
        return session.scalars(
            sa.select(ContentSourceRecord).where(ContentSourceRecord.source_id == source_id)
        ).first()

    def register_creator(self, creator: Creator) -> Creator:
        """Register one creator; a matching re-registration is a no-op."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = self._creator(session, creator.creator_id)
            if row is not None:
                stored = _creator_from_row(row)
                if (
                    stored.org_id != creator.org_id
                    or stored.display_name != creator.display_name
                    or stored.contact_ref != creator.contact_ref
                    or stored.metadata != creator.metadata
                ):
                    raise ValueError(
                        f"creator already registered with different fields: {creator.creator_id}"
                    )
                return stored
            row = CreatorRecord(
                creator_id=creator.creator_id,
                org_id=creator.org_id,
                display_name=creator.display_name,
                contact_ref=creator.contact_ref,
                creator_metadata=creator.metadata,
                created_at=now,
            )
            session.add(row)
            session.flush()
            _audit(
                "rights_entities.creator_registered",
                f"creator:{creator.creator_id}",
                {"org_id": creator.org_id, "contact_ref": creator.contact_ref},
            )
            return _creator_from_row(row)

    def get_creator(self, creator_id: str) -> Creator | None:
        """Return one creator, or None when it is not registered."""
        with self._sessions() as session:
            row = self._creator(session, creator_id)
            return _creator_from_row(row) if row is not None else None

    def list_creators(self, org_id: str) -> list[Creator]:
        """Return creators for one organization, ordered by creator_id."""
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(CreatorRecord)
                .where(CreatorRecord.org_id == org_id)
                .order_by(CreatorRecord.creator_id)
            ).all()
            return [_creator_from_row(row) for row in rows]

    def register_source(self, source: ContentSource) -> ContentSource:
        """Register one source; creator must exist and re-registration must match."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            creator = self._creator(session, source.creator_id)
            if creator is None:
                raise ValueError(f"creator not found: {source.creator_id}")
            if creator.org_id != source.org_id:
                raise ValueError("creator belongs to a different organization")
            row = self._source(session, source.source_id)
            if row is not None:
                stored = _source_from_row(row)
                if (
                    stored.org_id != source.org_id
                    or stored.creator_id != source.creator_id
                    or stored.source_type != source.source_type
                    or stored.canonical_url != source.canonical_url
                    or stored.metadata != source.metadata
                ):
                    raise ValueError(
                        f"source already registered with different fields: {source.source_id}"
                    )
                return stored
            row = ContentSourceRecord(
                source_id=source.source_id,
                org_id=source.org_id,
                creator_id=source.creator_id,
                source_type=source.source_type.value,
                canonical_url=source.canonical_url,
                active=source.active,
                source_metadata=source.metadata,
                created_at=now,
            )
            session.add(row)
            session.flush()
            _audit(
                "rights_entities.source_registered",
                f"source:{source.source_id}",
                {
                    "org_id": source.org_id,
                    "creator_id": source.creator_id,
                    "source_type": source.source_type.value,
                },
            )
            return _source_from_row(row)

    def get_source(self, source_id: str) -> ContentSource | None:
        """Return one source, or None when it is not registered."""
        with self._sessions() as session:
            row = self._source(session, source_id)
            return _source_from_row(row) if row is not None else None

    def list_sources(self, org_id: str, active_only: bool = False) -> list[ContentSource]:
        """Return org-scoped sources ordered by source_id; optionally active only."""
        with self._sessions() as session:
            query = sa.select(ContentSourceRecord).where(ContentSourceRecord.org_id == org_id)
            if active_only:
                query = query.where(ContentSourceRecord.active.is_(True))
            rows = session.scalars(query.order_by(ContentSourceRecord.source_id)).all()
            return [_source_from_row(row) for row in rows]

    def deactivate_source(self, source_id: str, org_id: str) -> ContentSource:
        """Deactivate one org-scoped source; deactivating twice is a no-op."""
        with self._sessions.begin() as session:
            row = self._source(session, source_id)
            if row is None or row.org_id != org_id:
                raise ValueError(f"source not found: {source_id}")
            row.active = False
            session.flush()
            stored = _source_from_row(row)
        _audit(
            "rights_entities.source_deactivated",
            f"source:{source_id}",
            {"org_id": org_id, "active": False},
        )
        return stored

    def register_item(self, item: SourceItem) -> SourceItem:
        """Register one source item; duplicates on (source_id, external_ref) return existing."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            source = self._source(session, item.source_id)
            if source is None:
                raise ValueError(f"source not found: {item.source_id}")
            if source.org_id != item.org_id:
                raise ValueError("source belongs to a different organization")
            existing = session.scalars(
                sa.select(SourceItemRecord).where(
                    SourceItemRecord.source_id == item.source_id,
                    SourceItemRecord.external_ref == item.external_ref,
                )
            ).first()
            if existing is not None:
                stored = _item_from_row(existing)
                if (
                    stored.org_id != item.org_id
                    or stored.content_ref != item.content_ref
                    or stored.metadata != item.metadata
                ):
                    raise ValueError(
                        "source item already registered with different fields: "
                        f"{item.source_id}:{item.external_ref}"
                    )
                return stored
            row = SourceItemRecord(
                item_id=item.item_id,
                org_id=item.org_id,
                source_id=item.source_id,
                external_ref=item.external_ref,
                captured_at=item.captured_at,
                content_ref=item.content_ref,
                item_metadata=item.metadata,
                created_at=now,
            )
            session.add(row)
            session.flush()
            _audit(
                "rights_entities.item_registered",
                f"item:{item.source_id}:{item.external_ref}",
                {"org_id": item.org_id, "item_id": item.item_id},
            )
            return _item_from_row(row)

    def list_items(self, source_id: str, org_id: str) -> list[SourceItem]:
        """Return org-scoped items for one source ordered by external_ref."""
        with self._sessions() as session:
            source = self._source(session, source_id)
            if source is None or source.org_id != org_id:
                raise ValueError(f"source not found: {source_id}")
            rows = session.scalars(
                sa.select(SourceItemRecord)
                .where(SourceItemRecord.source_id == source_id, SourceItemRecord.org_id == org_id)
                .order_by(SourceItemRecord.external_ref)
            ).all()
            return [_item_from_row(row) for row in rows]


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> RightsEntitiesStore:
    return RightsEntitiesStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def register_creator_route(
    request: Creator,
    store: Annotated[RightsEntitiesStore, Depends(_store)],
) -> Creator:
    """Register one creator (idempotent on matching definition)."""
    try:
        return store.register_creator(request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def get_creator_route(
    creator_id: str,
    org_id: Annotated[str, Query(min_length=1)],
    store: Annotated[RightsEntitiesStore, Depends(_store)],
) -> Creator:
    """Return one creator, or 404."""
    creator = store.get_creator(creator_id)
    if creator is None or creator.org_id != org_id:
        raise HTTPException(status_code=404, detail=f"creator not found: {creator_id}")
    return creator


def register_source_route(
    request: ContentSource,
    store: Annotated[RightsEntitiesStore, Depends(_store)],
) -> ContentSource:
    """Register one content source (creator must exist)."""
    try:
        return store.register_source(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def list_sources_route(
    store: Annotated[RightsEntitiesStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1)],
    active_only: Annotated[bool, Query()] = False,
) -> list[ContentSource]:
    """Return org-scoped content sources."""
    return store.list_sources(org_id, active_only=active_only)


def deactivate_source_route(
    source_id: str,
    request: DeactivateSourceRequest,
    store: Annotated[RightsEntitiesStore, Depends(_store)],
) -> ContentSource:
    """Deactivate one source (org-scoped request payload)."""
    try:
        return store.deactivate_source(source_id, request.org_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def register_item_route(
    request: SourceItem,
    store: Annotated[RightsEntitiesStore, Depends(_store)],
) -> SourceItem:
    """Register one source item; duplicates return the existing item."""
    try:
        return store.register_item(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def list_items_route(
    source_id: str,
    org_id: Annotated[str, Query(min_length=1)],
    store: Annotated[RightsEntitiesStore, Depends(_store)],
) -> list[SourceItem]:
    """Return org-scoped items for one source."""
    try:
        return store.list_items(source_id, org_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


router = APIRouter(tags=["rights-entities"])
router.add_api_route("/creators", register_creator_route, methods=["POST"], name="register_creator")
router.add_api_route(
    "/creators/{creator_id}", get_creator_route, methods=["GET"], name="get_creator"
)
router.add_api_route("/sources", register_source_route, methods=["POST"], name="register_source")
router.add_api_route("/sources", list_sources_route, methods=["GET"], name="list_sources")
router.add_api_route(
    "/sources/{source_id}/deactivate",
    deactivate_source_route,
    methods=["POST"],
    name="deactivate_source",
)
router.add_api_route("/source-items", register_item_route, methods=["POST"], name="register_item")
router.add_api_route(
    "/sources/{source_id}/items", list_items_route, methods=["GET"], name="list_items"
)
