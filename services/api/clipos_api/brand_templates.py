"""Brand/channel templates (CLIP-078, M19 opener).

PRD §E/J brand kit — reusable brand and per-channel posting templates
stored as **references and small config**, never secrets or asset
bytes:

- **Brand templates** — `brand_templates` rows are org-scoped,
  idempotent per (org_id, template_id) with slug ids: a title, an
  optional voice_ref (voice asset reference), a color palette (list of
  short hex strings, validated), and an optional logo_ref. Changed
  fields conflict (409). Audited `brand_templates.registered`.
- **Channel templates** — `channel_templates` are org-scoped posting
  templates for one platform slug: default Visibility from the
  CLIP-058 publishing contract, optional caption_text (config, <=
  1000 chars), hashtags list (<= 16, each 1..64), and an optional
  brand_template_id that must reference an existing brand template in
  the same org (404 otherwise). Idempotent with conflict (409);
  audited `channel_templates.registered`.
- Palette entries must match `#RGB` or `#RRGGBB`; hashtags must start
  with `#` and contain no whitespace (validated at the contract layer,
  so bad input fails 422 before any persistence).

Migration 0056 (CREATE-only). Routes (CLIP-033 pattern): POST/GET
/brand-templates, GET /brand-templates/{template_id}, POST/GET
/channel-templates, GET /channel-templates/{template_id}. No LLM, no
network.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.publishing import Visibility
from clipos_api.repository import Base

HEX_PATTERN = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"
HASHTAG_PATTERN = r"^#[^\s#]{1,63}$"


class BrandTemplate(BaseModel):
    """One reusable brand template (refs only, no asset bytes)."""

    model_config = ConfigDict(frozen=True)

    template_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    voice_ref: str | None = Field(default=None, max_length=255)
    color_palette: list[str] = Field(default_factory=list, max_length=16)
    logo_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = None


class ChannelTemplate(BaseModel):
    """One per-platform posting template."""

    model_config = ConfigDict(frozen=True)

    template_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    default_visibility: Visibility
    caption_text: str = Field(default="", max_length=1000)
    hashtags: list[str] = Field(default_factory=list, max_length=16)
    brand_template_id: str | None = Field(default=None, max_length=128)
    created_at: datetime | None = None


class RegisterBrandTemplateRequest(BaseModel):
    """Request body for registering one brand template."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    template_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    voice_ref: str | None = Field(default=None, max_length=255)
    color_palette: list[str] = Field(default_factory=list, max_length=16)
    logo_ref: str | None = Field(default=None, max_length=512)

    @field_validator("color_palette")
    @classmethod
    def _validate_palette(cls, value: list[str]) -> list[str]:
        for color in value:
            if not re.fullmatch(HEX_PATTERN, color):
                raise ValueError(f"invalid hex color: {color}")
        return value


class RegisterChannelTemplateRequest(BaseModel):
    """Request body for registering one channel template."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    template_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    platform: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    default_visibility: Visibility = Visibility.PRIVATE
    caption_text: str = Field(default="", max_length=1000)
    hashtags: list[str] = Field(default_factory=list, max_length=16)
    brand_template_id: str | None = Field(default=None, max_length=128)

    @field_validator("hashtags")
    @classmethod
    def _validate_hashtags(cls, value: list[str]) -> list[str]:
        for tag in value:
            if not re.fullmatch(HASHTAG_PATTERN, tag):
                raise ValueError(f"invalid hashtag: {tag}")
        return value


class BrandTemplateRecord(Base):
    """SQL row for one brand template."""

    __tablename__ = "brand_templates"
    __table_args__ = (
        sa.Index(
            "uq_brand_templates_org_template",
            "org_id",
            "template_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    voice_ref: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    color_palette: Mapped[list] = mapped_column(sa.JSON, nullable=False, default=list)
    logo_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ChannelTemplateRecord(Base):
    """SQL row for one channel template."""

    __tablename__ = "channel_templates"
    __table_args__ = (
        sa.Index(
            "uq_channel_templates_org_template",
            "org_id",
            "template_id",
            unique=True,
        ),
        sa.Index("ix_channel_templates_org_platform", "org_id", "platform"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    default_visibility: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    caption_text: Mapped[str] = mapped_column(sa.String(1000), nullable=False, default="")
    hashtags: Mapped[list] = mapped_column(sa.JSON, nullable=False, default=list)
    brand_template_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _brand_from_row(row: BrandTemplateRecord) -> BrandTemplate:
    return BrandTemplate(
        template_id=row.template_id,
        org_id=row.org_id,
        title=row.title,
        voice_ref=row.voice_ref,
        color_palette=list(row.color_palette or []),
        logo_ref=row.logo_ref,
        created_at=_aware(row.created_at),
    )


def _channel_from_row(row: ChannelTemplateRecord) -> ChannelTemplate:
    return ChannelTemplate(
        template_id=row.template_id,
        org_id=row.org_id,
        platform=row.platform,
        title=row.title,
        default_visibility=Visibility(row.default_visibility),
        caption_text=row.caption_text or "",
        hashtags=list(row.hashtags or []),
        brand_template_id=row.brand_template_id,
        created_at=_aware(row.created_at),
    )


class BrandTemplateStore:
    """Typed persistence for brand and channel templates."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> BrandTemplateStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_brand(self, request: RegisterBrandTemplateRequest) -> BrandTemplate:
        """Idempotent per (org, template_id); changed fields conflict."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(BrandTemplateRecord).where(
                    BrandTemplateRecord.org_id == request.org_id,
                    BrandTemplateRecord.template_id == request.template_id,
                )
            ).first()
            if existing is not None:
                stored = _brand_from_row(existing)
                if (
                    stored.title != request.title
                    or stored.voice_ref != request.voice_ref
                    or stored.color_palette != request.color_palette
                    or stored.logo_ref != request.logo_ref
                ):
                    raise ValueError(
                        f"brand template already registered with different fields: "
                        f"{request.template_id}"
                    )
                return stored
            row = BrandTemplateRecord(
                template_id=request.template_id,
                org_id=request.org_id,
                title=request.title,
                voice_ref=request.voice_ref,
                color_palette=list(request.color_palette),
                logo_ref=request.logo_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "brand_templates.registered",
                "brand_templates",
                f"brand_template:{request.template_id}",
                {"org_id": request.org_id, "title": request.title},
            )
            return _brand_from_row(row)

    def get_brand(self, template_id: str, org_id: str) -> BrandTemplate | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(BrandTemplateRecord).where(
                    BrandTemplateRecord.template_id == template_id,
                    BrandTemplateRecord.org_id == org_id,
                )
            ).first()
            return _brand_from_row(row) if row is not None else None

    def list_brands(self, org_id: str) -> list[BrandTemplate]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(BrandTemplateRecord)
                .where(BrandTemplateRecord.org_id == org_id)
                .order_by(BrandTemplateRecord.created_at, BrandTemplateRecord.id)
            ).all()
            return [_brand_from_row(r) for r in rows]

    def register_channel(self, request: RegisterChannelTemplateRequest) -> ChannelTemplate:
        """Idempotent per (org, template_id); brand link validated."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            if request.brand_template_id is not None:
                brand = session.scalars(
                    sa.select(BrandTemplateRecord).where(
                        BrandTemplateRecord.org_id == request.org_id,
                        BrandTemplateRecord.template_id == request.brand_template_id,
                    )
                ).first()
                if brand is None:
                    raise ValueError(f"brand template not found: {request.brand_template_id}")
            existing = session.scalars(
                sa.select(ChannelTemplateRecord).where(
                    ChannelTemplateRecord.org_id == request.org_id,
                    ChannelTemplateRecord.template_id == request.template_id,
                )
            ).first()
            if existing is not None:
                stored = _channel_from_row(existing)
                if (
                    stored.platform != request.platform
                    or stored.title != request.title
                    or stored.default_visibility != request.default_visibility
                    or stored.caption_text != request.caption_text
                    or stored.hashtags != request.hashtags
                    or stored.brand_template_id != request.brand_template_id
                ):
                    raise ValueError(
                        f"channel template already registered with different fields: "
                        f"{request.template_id}"
                    )
                return stored
            row = ChannelTemplateRecord(
                template_id=request.template_id,
                org_id=request.org_id,
                platform=request.platform,
                title=request.title,
                default_visibility=request.default_visibility.value,
                caption_text=request.caption_text,
                hashtags=list(request.hashtags),
                brand_template_id=request.brand_template_id,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "channel_templates.registered",
                "channel_templates",
                f"channel_template:{request.template_id}",
                {
                    "org_id": request.org_id,
                    "platform": request.platform,
                    "default_visibility": request.default_visibility.value,
                },
            )
            return _channel_from_row(row)

    def get_channel(self, template_id: str, org_id: str) -> ChannelTemplate | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ChannelTemplateRecord).where(
                    ChannelTemplateRecord.template_id == template_id,
                    ChannelTemplateRecord.org_id == org_id,
                )
            ).first()
            return _channel_from_row(row) if row is not None else None

    def list_channels(self, org_id: str, platform: str | None = None) -> list[ChannelTemplate]:
        with self._sessions() as session:
            stmt = sa.select(ChannelTemplateRecord).where(ChannelTemplateRecord.org_id == org_id)
            if platform is not None:
                stmt = stmt.where(ChannelTemplateRecord.platform == platform)
            rows = session.scalars(
                stmt.order_by(ChannelTemplateRecord.created_at, ChannelTemplateRecord.id)
            ).all()
            return [_channel_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["brand-templates"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> BrandTemplateStore:
    return BrandTemplateStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post(
    "/brand-templates",
    response_model=BrandTemplate,
    name="register_brand_template",
)
def register_brand_template_route(
    request: RegisterBrandTemplateRequest,
    store: Annotated[BrandTemplateStore, Depends(_store)],
) -> BrandTemplate:
    """Register one brand template (idempotent per org+template_id)."""
    try:
        return store.register_brand(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/brand-templates/{template_id}",
    response_model=BrandTemplate,
    name="get_brand_template",
)
def get_brand_template_route(
    template_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[BrandTemplateStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> BrandTemplate:
    """Get one brand template by id and org."""
    stored = store.get_brand(template_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"brand template not found: {template_id}")
    return stored


@router.get(
    "/brand-templates",
    response_model=list[BrandTemplate],
    name="list_brand_templates",
)
def list_brand_templates_route(
    store: Annotated[BrandTemplateStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[BrandTemplate]:
    """List brand templates for one org."""
    return store.list_brands(org_id)


@router.post(
    "/channel-templates",
    response_model=ChannelTemplate,
    name="register_channel_template",
)
def register_channel_template_route(
    request: RegisterChannelTemplateRequest,
    store: Annotated[BrandTemplateStore, Depends(_store)],
) -> ChannelTemplate:
    """Register one channel template (brand link validated; idempotent)."""
    try:
        return store.register_channel(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/channel-templates/{template_id}",
    response_model=ChannelTemplate,
    name="get_channel_template",
)
def get_channel_template_route(
    template_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[BrandTemplateStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> ChannelTemplate:
    """Get one channel template by id and org."""
    stored = store.get_channel(template_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"channel template not found: {template_id}")
    return stored


@router.get(
    "/channel-templates",
    response_model=list[ChannelTemplate],
    name="list_channel_templates",
)
def list_channel_templates_route(
    store: Annotated[BrandTemplateStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    platform: Annotated[str | None, Query(max_length=64)] = None,
) -> list[ChannelTemplate]:
    """List channel templates for one org, optionally by platform."""
    return store.list_channels(org_id, platform)
