"""TTS provider adapter and voice assets (CLIP-054).

M13 opener following the CLIP-040 adapter discipline: a provider-neutral
``TTSAdapter`` Protocol, a deterministic ``MockTTSAdapter`` that emits a
real WAV of proportional silence using only the stdlib ``wave`` module
(no network, no vendor SDK), and ``voice_assets`` persistence where every
asset carries a caller-supplied **Model Registry alias**
(``model_alias`` — required, never defaulted, never a vendor identifier)
plus a provider-neutral ``voice_ref``.

Synthesis writes to a workdir-confined path (CLIP-039 policy, shared
render workdir) and audits ``voice_synthesis.completed`` with the alias
and byte count; registration audits ``voice_assets.registered`` and is
idempotent per ``(org_id, voice_asset_id)`` with conflict on changed
fields. The route executes the Mock adapter as the deterministic
stand-in; real provider adapters register behind the same Protocol in
later M13 tickets, dispatched by ``model_alias`` — no adapter hardcodes
a provider name here. Text is bounded 1..4000 chars; duration derives
deterministically from length (15 chars/sec, clamped 0.5s..15s).
"""

from __future__ import annotations

import io
import wave
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol

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
    relative_ref,
)
from clipos_api.media_ingestion import MediaAssetKind
from clipos_api.render_pipeline import confine_path
from clipos_api.repository import Base

#: Mock synthesis pacing: characters per second.
CHARS_PER_SECOND = 15

#: Mock duration bounds (ms).
MIN_SYNTHESIS_MS = 500
MAX_SYNTHESIS_MS = 15_000

#: Mock WAV format: 8 kHz mono 16-bit silence keeps files tiny.
MOCK_SAMPLE_RATE = 800


def synthesis_duration_ms(text: str) -> int:
    """Deterministic mock duration for one text (pure, 15 chars/sec)."""
    return max(MIN_SYNTHESIS_MS, min(MAX_SYNTHESIS_MS, int(len(text) * 1000 / CHARS_PER_SECOND)))


def silence_wav(duration_ms: int, sample_rate: int = MOCK_SAMPLE_RATE) -> bytes:
    """Valid WAV bytes of silence for one duration (pure, stdlib only)."""
    frames = max(1, sample_rate * duration_ms // 1000)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


class TTSAdapter(Protocol):
    """Provider-neutral synthesis backend keyed by registry alias usage."""

    def synthesize(self, text: str, voice_ref: str, model_alias: str) -> bytes: ...


class MockTTSAdapter:
    """Deterministic stand-in: valid silence WAV sized from the text."""

    def synthesize(self, text: str, voice_ref: str, model_alias: str) -> bytes:
        # voice_ref/model_alias accepted for Protocol parity; the mock
        # performs no dispatch — real adapters resolve them later.
        return silence_wav(synthesis_duration_ms(text))


class VoiceAsset(BaseModel):
    """One voice asset; natural key = (org_id, voice_asset_id)."""

    model_config = ConfigDict(frozen=True)

    voice_asset_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=255)
    voice_ref: str = Field(min_length=1, max_length=255)
    model_alias: str = Field(min_length=1, max_length=128)
    created_at: datetime | None = None


class VoiceSynthesisRequest(BaseModel):
    """Request body for synthesizing speech for one voice asset."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4000)
    output_path: str = Field(min_length=1, max_length=1024)


class VoiceSynthesisResult(BaseModel):
    """Outcome of one deterministic synthesis run."""

    model_config = ConfigDict(frozen=True)

    voice_asset_id: str = Field(min_length=1, max_length=128)
    model_alias: str = Field(min_length=1, max_length=128)
    output_path: str = Field(min_length=1, max_length=1024)
    bytes_written: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    adapter_kind: Literal["mock", "real"] = "real"


class VoiceAssetRecord(Base):
    """SQL row for one voice asset."""

    __tablename__ = "voice_assets"
    __table_args__ = (
        sa.Index("uq_voice_assets_org_voice", "org_id", "voice_asset_id", unique=True),
        sa.Index("ix_voice_assets_org", "org_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    voice_asset_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    label: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    voice_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    model_alias: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _asset_from_row(row: VoiceAssetRecord) -> VoiceAsset:
    return VoiceAsset(
        voice_asset_id=row.voice_asset_id,
        org_id=row.org_id,
        label=row.label,
        voice_ref=row.voice_ref,
        model_alias=row.model_alias,
        created_at=_aware(row.created_at),
    )


class VoiceAssetStore:
    """Typed persistence for voice assets."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> VoiceAssetStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_asset(self, asset: VoiceAsset) -> VoiceAsset:
        """Idempotent register per (org, voice_asset_id); conflict on change."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(VoiceAssetRecord).where(
                    VoiceAssetRecord.org_id == asset.org_id,
                    VoiceAssetRecord.voice_asset_id == asset.voice_asset_id,
                )
            ).first()
            if existing is not None:
                stored = _asset_from_row(existing)
                if (
                    stored.label != asset.label
                    or stored.voice_ref != asset.voice_ref
                    or stored.model_alias != asset.model_alias
                ):
                    raise ValueError(
                        f"voice asset already registered with different fields: "
                        f"{asset.voice_asset_id}"
                    )
                return stored
            row = VoiceAssetRecord(
                voice_asset_id=asset.voice_asset_id,
                org_id=asset.org_id,
                label=asset.label,
                voice_ref=asset.voice_ref,
                model_alias=asset.model_alias,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "voice_assets.registered",
                "voice_assets",
                f"voice_asset:{asset.voice_asset_id}",
                {
                    "org_id": asset.org_id,
                    "label": asset.label,
                    "model_alias": asset.model_alias,
                },
            )
            return _asset_from_row(row)

    def get_asset(self, voice_asset_id: str, org_id: str) -> VoiceAsset | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(VoiceAssetRecord).where(
                    VoiceAssetRecord.voice_asset_id == voice_asset_id,
                    VoiceAssetRecord.org_id == org_id,
                )
            ).first()
            return _asset_from_row(row) if row is not None else None

    def list_assets(self, org_id: str) -> list[VoiceAsset]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(VoiceAssetRecord)
                .where(VoiceAssetRecord.org_id == org_id)
                .order_by(VoiceAssetRecord.created_at, VoiceAssetRecord.id)
            ).all()
            return [_asset_from_row(row) for row in rows]


def synthesize_speech(
    adapter: TTSAdapter,
    store: VoiceAssetStore,
    voice_asset_id: str,
    request: VoiceSynthesisRequest,
) -> VoiceSynthesisResult:
    """Synthesize one text to a workdir-confined WAV (deterministic mock).

    Asset must exist (404); output resolves inside RENDER_WORKDIR (422).
    Writes silence WAV sized from the text and audits
    ``voice_synthesis.completed`` with alias + byte count.
    """
    asset = store.get_asset(voice_asset_id, request.org_id)
    if asset is None:
        raise ValueError(f"voice asset not found: {voice_asset_id}")
    output = confine_path(request.output_path)
    payload = adapter.synthesize(request.text, asset.voice_ref, asset.model_alias)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
    except OSError as exc:
        raise ValueError(f"synthesis write failed: {exc}") from exc
    duration = synthesis_duration_ms(request.text)
    audit.record(
        "voice_synthesis.completed",
        "voice_synthesis",
        f"voice_asset:{asset.voice_asset_id}",
        {
            "org_id": request.org_id,
            "voice_asset_id": asset.voice_asset_id,
            "model_alias": asset.model_alias,
            "bytes_written": len(payload),
            "duration_ms": duration,
            "output_path": str(output),
        },
    )
    return VoiceSynthesisResult(
        voice_asset_id=asset.voice_asset_id,
        model_alias=asset.model_alias,
        output_path=str(output),
        bytes_written=len(payload),
        duration_ms=duration,
    )


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["voice-assets"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> VoiceAssetStore:
    return VoiceAssetStore.from_engine(engine)


def _labels(engine: Annotated[Engine, Depends(_database_engine)]) -> GeneratedMediaStore:
    return GeneratedMediaStore.from_engine(engine)


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
    "/voice-assets",
    response_model=VoiceAsset,
    name="register_voice_asset",
)
def register_voice_asset_route(
    request: VoiceAsset,
    store: Annotated[VoiceAssetStore, Depends(_store)],
) -> VoiceAsset:
    """Register one voice asset (idempotent; model_alias required)."""
    try:
        return store.register_asset(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/voice-assets/{voice_asset_id}",
    response_model=VoiceAsset,
    name="get_voice_asset",
)
def get_voice_asset_route(
    voice_asset_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[VoiceAssetStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> VoiceAsset:
    """Get one voice asset by id and org."""
    stored = store.get_asset(voice_asset_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"voice asset not found: {voice_asset_id}")
    return stored


@router.get(
    "/voice-assets",
    response_model=list[VoiceAsset],
    name="list_voice_assets",
)
def list_voice_assets_route(
    store: Annotated[VoiceAssetStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[VoiceAsset]:
    """List voice assets for one org."""
    return store.list_assets(org_id)


@router.post(
    "/voice-assets/{voice_asset_id}/synthesize",
    response_model=VoiceSynthesisResult,
    name="synthesize_voice_asset",
)
def synthesize_voice_asset_route(
    request: VoiceSynthesisRequest,
    voice_asset_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[VoiceAssetStore, Depends(_store)],
    labels: Annotated[GeneratedMediaStore, Depends(_labels)],
) -> VoiceSynthesisResult:
    """Synthesize speech to a confined WAV (deterministic Mock adapter).

    The MockTTSAdapter is the stand-in until real provider adapters
    register behind the TTSAdapter Protocol (later M13 tickets,
    dispatched by the asset's model_alias). output_path must already
    resolve inside RENDER_WORKDIR (422 otherwise). Every successful
    synthesis is auto-labeled AI_GENERATED (CLIP-057) with the asset's
    registry alias.
    """
    try:
        result = synthesize_speech(MockTTSAdapter(), store, voice_asset_id, request)
        record_generated_media(
            labels,
            GeneratedMediaMetadataRequest(
                org_id=request.org_id,
                asset_kind=MediaAssetKind.AUDIO,
                asset_ref=relative_ref(result.output_path),
                generator_alias=result.model_alias,
            ),
        )
        return result
    except ValueError as exc:
        raise _map_error(exc) from exc
