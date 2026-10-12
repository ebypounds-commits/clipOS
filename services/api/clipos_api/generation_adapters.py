"""Image/video generation adapters (CLIP-055).

M13 generation layer following the CLIP-040/054 adapter discipline:
provider-neutral ``ImageGenAdapter`` / ``VideoGenAdapter`` Protocols with
deterministic Mock implementations and **no provider identifier
anywhere** — both mocks take the caller's Model Registry alias purely as
a provenance reference (real adapters register behind the Protocols in
later tickets, dispatched by that alias):

- ``MockImageGenAdapter`` emits a valid solid-color PNG using only the
  stdlib (struct/zlib); the RGB color derives deterministically from
  sha256(prompt) so identical prompts yield byte-identical PNGs.
- ``MockVideoGenAdapter`` renders a black ``color=`` clip via ffmpeg
  lavfi (system binary, argv-only, 60s timeout; missing/failing ffmpeg
  -> ValueError -> 422).

Persistence: ``image_assets`` (migration 0036) is idempotent per
``(org_id, image_asset_id)`` with a content brief gate, prompt
(<=4000 chars), required ``model_alias``, dimensions 64..2048, and a
workdir-relative ``output_path``. Video generation writes into the
CLIP-038 chain: ``MediaIngestionStore.register_asset`` (kind=VIDEO,
status=READY) + ``record_version`` (v1 with storage_ref, bytes, and
sha256 checksum — idempotent, monotonic max+1). Both flows are
idempotent: an identical existing row returns without regenerating.

Audits: ``image_assets.generated``, ``video_generations.generated``
(plus the media-ingestion store's own asset/version audits). Output
paths are RENDER_WORKDIR-confined (CLIP-039 policy). No LLM.
"""

from __future__ import annotations

import hashlib
import struct
import subprocess
import tempfile
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.ai_labels import (
    GeneratedMediaMetadataRequest,
    GeneratedMediaStore,
    record_generated_media,
)
from clipos_api.content_evals import ContentEvalStore
from clipos_api.content_planning import ContentBriefRecord
from clipos_api.media_ingestion import (
    AssetVersion,
    MediaAsset,
    MediaAssetKind,
    MediaAssetStatus,
    MediaIngestionStore,
)
from clipos_api.render_pipeline import confine_path, render_workdir
from clipos_api.repository import Base

#: Mock video generation bounds (ms) — the mock renders 1..30s clips.
MIN_VIDEO_MS = 1000
MAX_VIDEO_MS = 60_000
MOCK_VIDEO_MAX_SECONDS = 30


class ImageGenAdapter(Protocol):
    """Provider-neutral image generation backend."""

    def generate_image(self, prompt: str, model_alias: str, width: int, height: int) -> bytes: ...


class VideoGenAdapter(Protocol):
    """Provider-neutral video generation backend."""

    def generate_video(self, prompt: str, model_alias: str, duration_ms: int) -> bytes: ...


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Valid 8-bit RGB PNG of one solid color (pure stdlib, deterministic)."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    idat = zlib.compress(raw, 9)
    return (
        signature + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")
    )


def prompt_color(prompt: str) -> tuple[int, int, int]:
    """Deterministic RGB triple derived from sha256(prompt) (pure)."""
    digest = hashlib.sha256(prompt.encode()).digest()
    return digest[0], digest[1], digest[2]


class MockImageGenAdapter:
    """Deterministic stand-in: solid PNG colored from the prompt hash."""

    def generate_image(self, prompt: str, model_alias: str, width: int, height: int) -> bytes:
        # model_alias accepted for Protocol parity; no dispatch in the mock.
        return solid_png(width, height, prompt_color(prompt))


class MockVideoGenAdapter:
    """Deterministic stand-in: ffmpeg lavfi black clip -> mp4 bytes."""

    def generate_video(self, prompt: str, model_alias: str, duration_ms: int) -> bytes:
        seconds = max(1, min(MOCK_VIDEO_MAX_SECONDS, duration_ms // 1000))
        with tempfile.TemporaryDirectory(prefix="clipos-gen-") as tmp:
            target = Path(tmp) / "gen.mp4"
            argv = [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s=320x240:d={seconds}",
                "-pix_fmt",
                "yuv420p",
                str(target),
            ]
            try:
                completed = subprocess.run(
                    argv, capture_output=True, text=True, timeout=60, check=False
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError(f"ffmpeg unavailable for mock video generation: {exc}") from exc
            if completed.returncode != 0:
                raise ValueError(
                    f"ffmpeg mock video generation failed: {(completed.stderr or '').strip()[:200]}"
                )
            return target.read_bytes()


class ImageAsset(BaseModel):
    """One generated image asset; natural key = (org_id, image_asset_id)."""

    model_config = ConfigDict(frozen=True)

    image_asset_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    brief_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=4000)
    model_alias: str = Field(min_length=1, max_length=128)
    width: int = Field(default=512, ge=64, le=2048)
    height: int = Field(default=512, ge=64, le=2048)
    output_path: str = Field(min_length=1, max_length=512)
    created_at: datetime | None = None


class ImageGenerationRequest(BaseModel):
    """Request body for one image generation run."""

    model_config = ConfigDict(frozen=True)

    image_asset_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    brief_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=4000)
    model_alias: str = Field(min_length=1, max_length=128)
    width: int = Field(default=512, ge=64, le=2048)
    height: int = Field(default=512, ge=64, le=2048)
    output_path: str = Field(min_length=1, max_length=1024)


class VideoGenerationRequest(BaseModel):
    """Request body for one video generation run."""

    model_config = ConfigDict(frozen=True)

    media_asset_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    brief_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    prompt: str = Field(min_length=1, max_length=4000)
    model_alias: str = Field(min_length=1, max_length=128)
    duration_ms: int = Field(default=2000, ge=MIN_VIDEO_MS, le=MAX_VIDEO_MS)
    output_path: str = Field(min_length=1, max_length=1024)


class VideoGenerationResult(BaseModel):
    """Outcome of one video generation run (media asset + version chain)."""

    model_config = ConfigDict(frozen=True)

    media_asset_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    storage_ref: str = Field(min_length=1, max_length=512)
    bytes: int = Field(ge=0)
    checksum_sha256: str = Field(min_length=64, max_length=64)


class ImageAssetRecord(Base):
    """SQL row for one generated image asset."""

    __tablename__ = "image_assets"
    __table_args__ = (
        sa.Index("uq_image_assets_org_image", "org_id", "image_asset_id", unique=True),
        sa.Index("ix_image_assets_org_brief", "org_id", "brief_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    image_asset_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    brief_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    prompt: Mapped[str] = mapped_column(sa.String(4000), nullable=False)
    model_alias: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    width: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    height: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    output_path: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _image_from_row(row: ImageAssetRecord) -> ImageAsset:
    return ImageAsset(
        image_asset_id=row.image_asset_id,
        org_id=row.org_id,
        brief_id=row.brief_id,
        prompt=row.prompt,
        model_alias=row.model_alias,
        width=row.width,
        height=row.height,
        output_path=row.output_path,
        created_at=_aware(row.created_at),
    )


def _require_brief(session: Session, brief_id: str, org_id: str) -> None:
    brief = session.scalars(
        sa.select(ContentBriefRecord).where(
            ContentBriefRecord.brief_id == brief_id,
            ContentBriefRecord.org_id == org_id,
        )
    ).first()
    if brief is None:
        raise ValueError(f"content brief not found: {brief_id}")


class GenerationStore:
    """Typed persistence for generated image assets."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> GenerationStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def get_image(self, image_asset_id: str, org_id: str) -> ImageAsset | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ImageAssetRecord).where(
                    ImageAssetRecord.image_asset_id == image_asset_id,
                    ImageAssetRecord.org_id == org_id,
                )
            ).first()
            return _image_from_row(row) if row is not None else None

    def list_images(self, org_id: str, brief_id: str) -> list[ImageAsset]:
        with self._sessions() as session:
            _require_brief(session, brief_id, org_id)
            rows = session.scalars(
                sa.select(ImageAssetRecord)
                .where(
                    ImageAssetRecord.org_id == org_id,
                    ImageAssetRecord.brief_id == brief_id,
                )
                .order_by(ImageAssetRecord.created_at, ImageAssetRecord.id)
            ).all()
            return [_image_from_row(row) for row in rows]


def generate_image(
    adapter: ImageGenAdapter,
    store: GenerationStore,
    request: ImageGenerationRequest,
) -> ImageAsset:
    """Generate one PNG for a brief (idempotent, confined, brief-gated).

    Identical existing row returns stored state without regenerating;
    changed prompt/alias/dimensions/path for the same id -> conflict
    (409). Unknown brief -> 404; output must resolve inside
    RENDER_WORKDIR (422).
    """
    now = datetime.now(UTC)
    output = confine_path(request.output_path)
    relative = str(output.relative_to(render_workdir()))
    existing = store.get_image(request.image_asset_id, request.org_id)
    if existing is not None:
        if (
            existing.brief_id != request.brief_id
            or existing.prompt != request.prompt
            or existing.model_alias != request.model_alias
            or existing.width != request.width
            or existing.height != request.height
            or existing.output_path != relative
        ):
            raise ValueError(
                f"image asset already generated with different fields: {request.image_asset_id}"
            )
        return existing

    with store._sessions.begin() as session:
        _require_brief(session, request.brief_id, request.org_id)

    payload = adapter.generate_image(
        request.prompt, request.model_alias, request.width, request.height
    )
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
    except OSError as exc:
        raise ValueError(f"image write failed: {exc}") from exc

    with store._sessions.begin() as session:
        row = ImageAssetRecord(
            image_asset_id=request.image_asset_id,
            org_id=request.org_id,
            brief_id=request.brief_id,
            prompt=request.prompt,
            model_alias=request.model_alias,
            width=request.width,
            height=request.height,
            output_path=relative,
            created_at=now,
        )
        session.add(row)
        session.flush()
        audit.record(
            "image_assets.generated",
            "image_assets",
            f"image_asset:{request.image_asset_id}",
            {
                "org_id": request.org_id,
                "brief_id": request.brief_id,
                "model_alias": request.model_alias,
                "width": request.width,
                "height": request.height,
                "bytes_written": len(payload),
            },
        )
        return _image_from_row(row)


def generate_video(
    adapter: VideoGenAdapter,
    ingestion: MediaIngestionStore,
    store: GenerationStore,
    request: VideoGenerationRequest,
) -> VideoGenerationResult:
    """Generate one video for a brief (idempotent, confined, brief-gated).

    Registers a READY VIDEO media asset + v1 asset_version (storage_ref =
    workdir-relative path, bytes, sha256) through the CLIP-038 chain.
    Identical existing asset returns stored version state without
    regenerating; changed fields -> conflict (409).
    """
    now = datetime.now(UTC)
    output = confine_path(request.output_path)
    relative = str(output.relative_to(render_workdir()))
    existing_asset = ingestion.get_asset(request.media_asset_id, request.org_id)
    if existing_asset is not None:
        if existing_asset.kind is not MediaAssetKind.VIDEO:
            raise ValueError(
                f"media asset already generated with different fields: {request.media_asset_id}"
            )
        versions = ingestion.list_versions(request.media_asset_id)
        if not versions:
            raise ValueError(
                f"media asset already generated with different fields: {request.media_asset_id}"
            )
        latest = max(versions, key=lambda version: version.version)
        if latest.storage_ref != relative or existing_asset.title != request.title:
            raise ValueError(
                f"media asset already generated with different fields: {request.media_asset_id}"
            )
        return VideoGenerationResult(
            media_asset_id=request.media_asset_id,
            org_id=request.org_id,
            storage_ref=latest.storage_ref,
            bytes=latest.bytes,
            checksum_sha256=latest.checksum_sha256,
        )

    with store._sessions.begin() as session:
        _require_brief(session, request.brief_id, request.org_id)

    payload = adapter.generate_video(request.prompt, request.model_alias, request.duration_ms)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
    except OSError as exc:
        raise ValueError(f"video write failed: {exc}") from exc

    checksum = hashlib.sha256(payload).hexdigest()
    asset = MediaAsset(
        media_asset_id=request.media_asset_id,
        org_id=request.org_id,
        source_item_id=None,
        kind=MediaAssetKind.VIDEO,
        title=request.title,
        status=MediaAssetStatus.READY,
        created_at=now,
    )
    ingestion.register_asset(asset)
    version = AssetVersion(
        media_asset_id=request.media_asset_id,
        org_id=request.org_id,
        version=1,
        storage_ref=relative,
        bytes=len(payload),
        checksum_sha256=checksum,
        created_at=now,
    )
    ingestion.record_version(version)
    audit.record(
        "video_generations.generated",
        "video_generations",
        f"media_asset:{request.media_asset_id}",
        {
            "org_id": request.org_id,
            "brief_id": request.brief_id,
            "model_alias": request.model_alias,
            "duration_ms": request.duration_ms,
            "bytes_written": len(payload),
            "checksum_sha256": checksum,
        },
    )
    return VideoGenerationResult(
        media_asset_id=request.media_asset_id,
        org_id=request.org_id,
        storage_ref=relative,
        bytes=len(payload),
        checksum_sha256=checksum,
    )


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["generation"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> GenerationStore:
    return GenerationStore.from_engine(engine)


def _ingestion(engine: Annotated[Engine, Depends(_database_engine)]) -> MediaIngestionStore:
    return MediaIngestionStore.from_engine(engine)


def _labels(engine: Annotated[Engine, Depends(_database_engine)]) -> GeneratedMediaStore:
    return GeneratedMediaStore.from_engine(engine)


def _transformations(engine: Annotated[Engine, Depends(_database_engine)]) -> ContentEvalStore:
    return ContentEvalStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        status = 404
    elif "already" in message:
        status = 409
    else:
        status = 422
    return HTTPException(status_code=status, detail=message)


@router.post(
    "/image-generations",
    response_model=ImageAsset,
    name="generate_image_route",
)
def generate_image_route(
    request: ImageGenerationRequest,
    store: Annotated[GenerationStore, Depends(_store)],
    labels: Annotated[GeneratedMediaStore, Depends(_labels)],
    transformations: Annotated[ContentEvalStore, Depends(_transformations)],
) -> ImageAsset:
    """Generate one PNG for a brief (Mock adapter; idempotent; auto-labeled)."""
    try:
        image = generate_image(MockImageGenAdapter(), store, request)
        record_generated_media(
            labels,
            GeneratedMediaMetadataRequest(
                org_id=image.org_id,
                brief_id=image.brief_id,
                asset_kind=MediaAssetKind.IMAGE,
                asset_ref=image.output_path,
                generator_alias=image.model_alias,
                prompt=image.prompt,
            ),
            transformation_store=transformations,
        )
        return image
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/image-generations/{image_asset_id}",
    response_model=ImageAsset,
    name="get_image_generation",
)
def get_image_generation_route(
    image_asset_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[GenerationStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> ImageAsset:
    """Get one generated image asset by id and org."""
    stored = store.get_image(image_asset_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"image asset not found: {image_asset_id}")
    return stored


@router.get(
    "/image-generations",
    response_model=list[ImageAsset],
    name="list_image_generations",
)
def list_image_generations_route(
    store: Annotated[GenerationStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    brief_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> list[ImageAsset]:
    """List generated images for one brief (404 when the brief is unknown)."""
    try:
        return store.list_images(org_id, brief_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/video-generations",
    response_model=VideoGenerationResult,
    name="generate_video_route",
)
def generate_video_route(
    request: VideoGenerationRequest,
    store: Annotated[GenerationStore, Depends(_store)],
    ingestion: Annotated[MediaIngestionStore, Depends(_ingestion)],
    labels: Annotated[GeneratedMediaStore, Depends(_labels)],
    transformations: Annotated[ContentEvalStore, Depends(_transformations)],
) -> VideoGenerationResult:
    """Generate one video for a brief (Mock adapter; chain v1; auto-labeled)."""
    try:
        result = generate_video(MockVideoGenAdapter(), ingestion, store, request)
        record_generated_media(
            labels,
            GeneratedMediaMetadataRequest(
                org_id=result.org_id,
                brief_id=request.brief_id,
                asset_kind=MediaAssetKind.VIDEO,
                asset_ref=result.storage_ref,
                generator_alias=request.model_alias,
                prompt=request.prompt,
            ),
            transformation_store=transformations,
        )
        return result
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/video-generations/{media_asset_id}",
    response_model=VideoGenerationResult,
    name="get_video_generation",
)
def get_video_generation_route(
    media_asset_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[GenerationStore, Depends(_store)],
    ingestion: Annotated[MediaIngestionStore, Depends(_ingestion)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> VideoGenerationResult:
    """Return the stored generation result for one media asset (404)."""
    asset = ingestion.get_asset(media_asset_id, org_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"media asset not found: {media_asset_id}")
    versions = ingestion.list_versions(media_asset_id)
    if not versions:
        raise HTTPException(status_code=404, detail=f"media asset not found: {media_asset_id}")
    latest = max(versions, key=lambda version: version.version)
    return VideoGenerationResult(
        media_asset_id=media_asset_id,
        org_id=org_id,
        storage_ref=latest.storage_ref,
        bytes=latest.bytes,
        checksum_sha256=latest.checksum_sha256,
    )
