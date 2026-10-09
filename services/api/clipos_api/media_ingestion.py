"""Authorized media ingestion: gate, media assets, and asset versions (CLIP-038).

Implements the ingestion-gate slice of the Media cluster:
- IngestionGate.authorize: ALLOWED only when the rights record for the source
  item has status CLEAR or basis GENERATED; otherwise DENIED with reason.
  Pure DB check — no network, no URL fetching.
- media_assets: org-scoped, idempotent per (org_id, media_asset_id).
- asset_versions: immutable per-asset content versions, idempotent per
  (media_asset_id, version); version numbers append-only and monotonic.
- ingest_asset: authorize first, then create/return the asset idempotently;
  DENIED raises PermissionError (route maps to 403).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, ClassVar
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base


class MediaAssetKind(str, Enum):
    """Kind of one media asset."""

    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    IMAGE = "IMAGE"


class MediaAssetStatus(str, Enum):
    """Lifecycle status of one media asset."""

    PENDING = "PENDING"
    READY = "READY"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class MediaAsset(BaseModel):
    """One org-scoped media asset."""

    model_config = ConfigDict(frozen=True)

    media_asset_id: str = Field(
        default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128
    )
    org_id: str = Field(min_length=1, max_length=64)
    source_item_id: str | None = Field(default=None, max_length=128)
    kind: MediaAssetKind = MediaAssetKind.VIDEO
    title: str = Field(min_length=1, max_length=255)
    status: MediaAssetStatus = MediaAssetStatus.PENDING
    created_at: datetime | None = None


class AssetVersion(BaseModel):
    """One immutable content version of a media asset."""

    model_config = ConfigDict(frozen=True)

    asset_version_id: str = Field(
        default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128
    )
    media_asset_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    storage_ref: str = Field(min_length=1, max_length=512)
    bytes: int = Field(ge=0)
    checksum_sha256: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    created_at: datetime | None = None


class IngestionDecision(BaseModel):
    """Outcome of the authorized-ingestion gate for one source item."""

    model_config = ConfigDict(frozen=True)

    source_item_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    allowed: bool
    reason: str = Field(min_length=1, max_length=255)


class IngestionRequest(BaseModel):
    """Request to ingest one media asset (gate applies before creation)."""

    model_config = ConfigDict(frozen=True)

    media_asset_id: str = Field(
        default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128
    )
    org_id: str = Field(min_length=1, max_length=64)
    source_item_id: str = Field(min_length=1, max_length=128)
    kind: MediaAssetKind = MediaAssetKind.VIDEO
    title: str = Field(min_length=1, max_length=255)


class MediaAssetRecord(Base):
    """SQL row for one media asset."""

    __tablename__ = "media_assets"
    __table_args__ = (
        sa.Index("uq_media_assets_org_asset", "org_id", "media_asset_id", unique=True),
        sa.Index("ix_media_assets_source_item", "source_item_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    media_asset_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_item_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class AssetVersionRecord(Base):
    """SQL row for one immutable asset version."""

    __tablename__ = "asset_versions"
    __table_args__ = (
        sa.Index("uq_asset_versions_version_id", "asset_version_id", unique=True),
        sa.Index(
            "uq_asset_versions_asset_version",
            "media_asset_id",
            "version",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    asset_version_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    media_asset_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    storage_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _asset_from_row(row: MediaAssetRecord) -> MediaAsset:
    return MediaAsset(
        media_asset_id=row.media_asset_id,
        org_id=row.org_id,
        source_item_id=row.source_item_id,
        kind=MediaAssetKind(row.kind),
        title=row.title,
        status=MediaAssetStatus(row.status),
        created_at=_aware(row.created_at),
    )


def _version_from_row(row: AssetVersionRecord) -> AssetVersion:
    return AssetVersion(
        asset_version_id=row.asset_version_id,
        media_asset_id=row.media_asset_id,
        org_id=row.org_id,
        version=row.version,
        storage_ref=row.storage_ref,
        bytes=row.bytes,
        checksum_sha256=row.checksum_sha256,
        created_at=_aware(row.created_at),
    )


class IngestionGate:
    """Authorized-ingestion gate over the rights ledger (pure DB check)."""

    ALLOWED_STATUSES: ClassVar[set[str]] = {"CLEAR"}
    ALLOWED_BASES: ClassVar[set[str]] = {"GENERATED"}

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> IngestionGate:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def authorize(self, source_item_id: str, org_id: str) -> IngestionDecision:
        from clipos_api.rights_ledger import RightsRecordRecord

        with self._sessions() as session:
            row = session.scalars(
                sa.select(RightsRecordRecord).where(
                    RightsRecordRecord.item_id == source_item_id,
                    RightsRecordRecord.org_id == org_id,
                )
            ).first()
        if row is None:
            return IngestionDecision(
                source_item_id=source_item_id,
                org_id=org_id,
                allowed=False,
                reason="no rights record for source item",
            )
        allowed = row.status in self.ALLOWED_STATUSES or row.basis in self.ALLOWED_BASES
        reason = (
            "rights record CLEAR or GENERATED basis"
            if allowed
            else f"rights record status={row.status} basis={row.basis}"
        )
        return IngestionDecision(
            source_item_id=source_item_id,
            org_id=org_id,
            allowed=allowed,
            reason=reason,
        )


class MediaIngestionStore:
    """Typed persistence for media assets and asset versions."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> MediaIngestionStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    def register_asset(self, asset: MediaAsset) -> MediaAsset:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(MediaAssetRecord).where(
                    MediaAssetRecord.media_asset_id == asset.media_asset_id,
                    MediaAssetRecord.org_id == asset.org_id,
                )
            ).first()
            if existing is not None:
                stored = _asset_from_row(existing)
                if (
                    stored.source_item_id != asset.source_item_id
                    or stored.kind != asset.kind
                    or stored.title != asset.title
                    or stored.status != asset.status
                ):
                    raise ValueError(
                        "media asset already registered with different fields: "
                        f"{asset.media_asset_id}"
                    )
                return stored
            row = MediaAssetRecord(
                media_asset_id=asset.media_asset_id,
                org_id=asset.org_id,
                source_item_id=asset.source_item_id,
                kind=asset.kind.value,
                title=asset.title,
                status=asset.status.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "media_ingestion.asset_registered",
                "media_ingestion",
                f"media_asset:{asset.media_asset_id}",
                {"org_id": asset.org_id, "kind": asset.kind.value},
            )
            return _asset_from_row(row)

    def get_asset(self, media_asset_id: str, org_id: str) -> MediaAsset | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(MediaAssetRecord).where(
                    MediaAssetRecord.media_asset_id == media_asset_id,
                    MediaAssetRecord.org_id == org_id,
                )
            ).first()
            return _asset_from_row(row) if row is not None else None

    def list_assets(self, org_id: str) -> list[MediaAsset]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(MediaAssetRecord)
                .where(MediaAssetRecord.org_id == org_id)
                .order_by(MediaAssetRecord.media_asset_id)
            ).all()
            return [_asset_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Versions (append-only)
    # ------------------------------------------------------------------

    def record_version(self, version: AssetVersion) -> AssetVersion:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            asset = session.scalars(
                sa.select(MediaAssetRecord).where(
                    MediaAssetRecord.media_asset_id == version.media_asset_id,
                    MediaAssetRecord.org_id == version.org_id,
                )
            ).first()
            if asset is None:
                raise ValueError(f"media asset not found: {version.media_asset_id}")
            existing = session.scalars(
                sa.select(AssetVersionRecord).where(
                    AssetVersionRecord.media_asset_id == version.media_asset_id,
                    AssetVersionRecord.version == version.version,
                )
            ).first()
            if existing is not None:
                stored = _version_from_row(existing)
                if (
                    stored.storage_ref != version.storage_ref
                    or stored.bytes != version.bytes
                    or stored.checksum_sha256 != version.checksum_sha256
                ):
                    raise ValueError(
                        "asset version already registered with different fields: "
                        f"{version.media_asset_id}/v{version.version}"
                    )
                return stored
            max_version = session.scalar(
                sa.select(sa.func.max(AssetVersionRecord.version)).where(
                    AssetVersionRecord.media_asset_id == version.media_asset_id
                )
            )
            if version.version != (max_version or 0) + 1:
                raise ValueError(
                    "asset version must be exactly max+1: "
                    f"{version.media_asset_id} v{version.version} (max={max_version or 0})"
                )
            row = AssetVersionRecord(
                asset_version_id=version.asset_version_id,
                media_asset_id=version.media_asset_id,
                org_id=version.org_id,
                version=version.version,
                storage_ref=version.storage_ref,
                bytes=version.bytes,
                checksum_sha256=version.checksum_sha256,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "media_ingestion.version_recorded",
                "media_ingestion",
                f"asset_version:{version.asset_version_id}",
                {
                    "org_id": version.org_id,
                    "media_asset_id": version.media_asset_id,
                    "version": version.version,
                },
            )
            return _version_from_row(row)

    def list_versions(self, media_asset_id: str) -> list[AssetVersion]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(AssetVersionRecord)
                .where(AssetVersionRecord.media_asset_id == media_asset_id)
                .order_by(AssetVersionRecord.version)
            ).all()
            return [_version_from_row(row) for row in rows]


def ingest_asset(
    gate: IngestionGate,
    store: MediaIngestionStore,
    request: IngestionRequest,
) -> tuple[IngestionDecision, MediaAsset]:
    """Authorize, then create/return the asset idempotently. DENIED -> PermissionError."""
    decision = gate.authorize(request.source_item_id, request.org_id)
    if not decision.allowed:
        raise PermissionError(decision.reason)
    asset = store.register_asset(
        MediaAsset(
            media_asset_id=request.media_asset_id,
            org_id=request.org_id,
            source_item_id=request.source_item_id,
            kind=request.kind,
            title=request.title,
        )
    )
    audit.record(
        "media_ingestion.gate_allowed",
        "media_ingestion",
        f"media_asset:{asset.media_asset_id}",
        {"org_id": request.org_id, "source_item_id": request.source_item_id},
    )
    return decision, asset


# ----------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ----------------------------------------------------------------------

router = APIRouter(tags=["media-ingestion"])


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> MediaIngestionStore:
    return MediaIngestionStore.from_engine(engine)


def _gate(engine: Annotated[Engine, Depends(_database_engine)]) -> IngestionGate:
    return IngestionGate.from_engine(engine)


@router.post("/media-assets", response_model=MediaAsset, name="ingest_media_asset")
def ingest_media_asset_route(
    request: IngestionRequest,
    gate: Annotated[IngestionGate, Depends(_gate)],
    store: Annotated[MediaIngestionStore, Depends(_store)],
) -> MediaAsset:
    try:
        _decision, asset = ingest_asset(gate, store, request)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return asset


@router.get(
    "/media-assets/{media_asset_id}",
    response_model=MediaAsset,
    name="get_media_asset",
)
def get_media_asset_route(
    media_asset_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[MediaIngestionStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> MediaAsset:
    stored = store.get_asset(media_asset_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"media asset not found: {media_asset_id}")
    return stored


@router.get("/media-assets", response_model=list[MediaAsset], name="list_media_assets")
def list_media_assets_route(
    store: Annotated[MediaIngestionStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[MediaAsset]:
    return store.list_assets(org_id)


@router.post(
    "/media-assets/{media_asset_id}/versions",
    response_model=AssetVersion,
    name="record_asset_version",
)
def record_asset_version_route(
    request: AssetVersion,
    media_asset_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[MediaIngestionStore, Depends(_store)],
) -> AssetVersion:
    if request.media_asset_id != media_asset_id:
        raise HTTPException(
            status_code=422,
            detail="version media_asset_id must match the path media_asset_id",
        )
    try:
        return store.record_version(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


@router.get(
    "/media-assets/{media_asset_id}/versions",
    response_model=list[AssetVersion],
    name="list_asset_versions",
)
def list_asset_versions_route(
    media_asset_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[MediaIngestionStore, Depends(_store)],
) -> list[AssetVersion]:
    return store.list_versions(media_asset_id)


@router.post(
    "/media-ingestion/authorize",
    response_model=IngestionDecision,
    name="authorize_ingestion",
)
def authorize_ingestion_route(
    gate: Annotated[IngestionGate, Depends(_gate)],
    source_item_id: Annotated[str, Query(min_length=1, max_length=128)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> IngestionDecision:
    return gate.authorize(source_item_id, org_id)
