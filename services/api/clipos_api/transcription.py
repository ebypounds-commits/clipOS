"""Transcription adapters and transcript storage.

This module provides:
- TranscriptionAdapter protocol (abstract, no vendor import)
- TranscriptionResult data contract
- MockTranscriptionAdapter for deterministic testing
- Flow helper transcribe_and_store

All user/source content is treated as untrusted input. No provider/model
identifiers are hard-coded; registry aliases are used instead.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field, ValidationInfo, field_validator
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError


class Segment(BaseModel):
    """A single transcript segment."""

    segment_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    seq: int = Field(..., ge=0)
    start_ms: int = Field(..., ge=0)
    end_ms: int = Field(..., ge=0)
    text: str = Field(..., max_length=2048)
    speaker: str | None = Field(default=None, max_length=128)

    @field_validator("end_ms")
    @classmethod
    def _end_not_before_start(cls, v: int, info: ValidationInfo) -> int:
        start = info.data.get("start_ms")
        if start is not None and v < start:
            raise ValueError("end_ms must be >= start_ms")
        return v


class TranscriptionResult(BaseModel):
    """Result returned by a TranscriptionAdapter."""

    language: str
    segments: list[Segment]
    provider_alias: str
    model_hint: str | None = Field(default=None, max_length=128)


class TranscriptionAdapter(Protocol):
    """Abstract contract for transcription adapters.

    Implementations must NOT import any vendor SDK. The only concrete adapter
    in this codebase is MockTranscriptionAdapter, used for tests.
    """

    def transcribe(self, storage_ref: str, language: str) -> TranscriptionResult:
        """Transcribe the media referenced by storage_ref in the given language.

        Returns a TranscriptionResult with segments, provider_alias, and
        optional model_hint.
        """
        ...


class MockTranscriptionAdapter:
    """Deterministic mock adapter for testing.

    Segments are generated with incrementing seq and timestamps over the
    provided canned text. No external calls are made.
    """

    def __init__(
        self,
        canned_text: str = "This is a test segment.",
        num_segments: int = 3,
    ) -> None:
        self.canned_text = canned_text
        self.num_segments = num_segments

    def transcribe(self, storage_ref: str, language: str) -> TranscriptionResult:
        segments: list[Segment] = []
        ref_hash = hashlib.sha256(storage_ref.encode("utf-8")).hexdigest()[:8]
        for i in range(self.num_segments):
            start_ms = i * 3333  # ~3.3s per segment
            end_ms = start_ms + 3333
            segments.append(
                Segment(
                    segment_id=f"seg-{ref_hash}-{i}",
                    seq=i,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=self.canned_text,
                    speaker=None,
                )
            )
        return TranscriptionResult(
            language=language,
            segments=segments,
            provider_alias="mock",
            model_hint=None,
        )


def transcribe_and_store(
    adapter: TranscriptionAdapter,
    store,
    media_asset_id: str,
    org_id: str,
    storage_ref: str,
    language: str,
) -> dict:
    """Transcribe media and persist transcript + segments.

    Authorize nothing (media already ingested). Call adapter.transcribe,
    persist transcript + segments, audit events:
    transcription.completed, transcription.segments_appended.

    Returns the transcript dict.
    """
    # Call adapter to transcribe
    result = adapter.transcribe(storage_ref, language)

    # Register transcript (idempotent — conflicts raise IntegrityError on dup org/media/lang)
    transcript = store.register_transcript(
        media_asset_id=media_asset_id,
        org_id=org_id,
        language=language,
        provider_alias=result.provider_alias,
        model_hint=result.model_hint,
    )

    # Record segments (append-only, seq monotonic — conflicts raise ValueError)
    recorded = store.record_segments(
        transcript_id=transcript["transcript_id"],
        org_id=org_id,
        segments=result.segments,
    )

    from clipos_api import audit as audit_mod

    audit_mod.record(
        "transcription.completed",
        "transcription",
        f"transcript:{transcript['transcript_id']}",
        {"org_id": org_id, "media_asset_id": media_asset_id, "segments": len(recorded)},
    )

    return transcript


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

from clipos_api.transcription_store import TranscriptionStore  # runtime import (PEP 563 safe)

router = APIRouter(tags=["transcription"])


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _store(
    engine: Annotated[Engine, Depends(_database_engine)],
) -> TranscriptionStore:
    from clipos_api.transcription_store import TranscriptionStore

    return TranscriptionStore.from_engine(engine)


class RegisterTranscriptRequest(BaseModel):
    """Request to register one transcript."""

    media_asset_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    language: str = Field(min_length=1, max_length=8)
    provider_alias: str = Field(min_length=1, max_length=128)
    model_hint: str | None = Field(default=None, max_length=128)


class RecordSegmentsRequest(BaseModel):
    """Request to append segments to a transcript."""

    org_id: str = Field(min_length=1, max_length=64)
    segments: list[Segment] = Field(min_length=1)


@router.post("/transcripts", name="register_transcript_route")
def register_transcript_route(
    request: RegisterTranscriptRequest,
    store: Annotated[TranscriptionStore, Depends(_store)],
) -> dict:
    try:
        return store.register_transcript(
            media_asset_id=request.media_asset_id,
            org_id=request.org_id,
            language=request.language,
            provider_alias=request.provider_alias,
            model_hint=request.model_hint,
        )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="transcript conflict") from exc


@router.post(
    "/transcripts/{transcript_id}/segments",
    name="record_segments_route",
)
def record_segments_route(
    transcript_id: Annotated[str, Path(min_length=1, max_length=64)],
    request: RecordSegmentsRequest,
    store: Annotated[TranscriptionStore, Depends(_store)],
) -> list[dict]:
    try:
        return store.record_segments(
            transcript_id=transcript_id,
            org_id=request.org_id,
            segments=request.segments,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/transcripts/{transcript_id}",
    name="get_transcript_route",
)
def get_transcript_route(
    transcript_id: Annotated[str, Path(min_length=1, max_length=64)],
    store: Annotated[TranscriptionStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> dict:
    stored = store.get_transcript(transcript_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="transcript not found")
    return stored


@router.get(
    "/transcripts/{transcript_id}/segments",
    name="list_segments_route",
)
def list_segments_route(
    transcript_id: Annotated[str, Path(min_length=1, max_length=64)],
    store: Annotated[TranscriptionStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[dict]:
    return store.list_segments(transcript_id, org_id)
