"""Clip candidate routes (CLIP-042)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine

from clipos_api.clip_candidates import (
    ClipCandidateSource,
    ClipCandidateStore,
    extract_and_register,
)
from clipos_api.repository import get_engine

router = APIRouter(tags=["clip-candidates"])


# ------------------------------------------------------------------
# Data models
# ------------------------------------------------------------------


class ClipCandidateCreate(BaseModel):
    """Request/response for one clip candidate."""

    model_config = ConfigDict(frozen=True)

    media_asset_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    start_ms: int = Field(ge=0, description="Start time in milliseconds")
    end_ms: int = Field(gt=0, description="End time in milliseconds")
    source: ClipCandidateSource = Field(default=ClipCandidateSource.SEMANTIC)
    reason: str = Field(default="heuristic extraction", max_length=255)


# Extract-and-register request body model
class ExtractAndRegisterRequest(BaseModel):
    """Request to extract and register clip candidates."""

    model_config = ConfigDict(frozen=True)

    media_asset_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    boundaries: list[tuple[int, int]] = Field(
        default=[],
        description="List of (start_ms, end_ms) segment boundaries",
    )
    min_len_ms: int = Field(default=1000, ge=0, description="Minimum clip length in ms")
    max_len_ms: int = Field(default=60000, ge=0, description="Maximum clip length in ms")
    min_gap_ms: int = Field(default=0, ge=0, description="Minimum gap between clips in ms")
    source: ClipCandidateSource = Field(default=ClipCandidateSource.SEMANTIC)
    reason: str = Field(default="heuristic extraction", max_length=255)


# Helper: get store per-request
# ------------------------------------------------------------------


def _get_store(engine: Annotated[Engine, Depends(get_engine)]) -> ClipCandidateStore:
    from clipos_api.clip_candidates import ClipCandidateStore

    return ClipCandidateStore.from_engine(engine)


# ------------------------------------------------------------------
# Router routes
# ------------------------------------------------------------------


@router.post(
    "/clip-candidates",
    response_model=ClipCandidateCreate,
    name="create_clip_candidate",
)
def create_clip_candidate_route(
    request: ClipCandidateCreate,
    store: Annotated[ClipCandidateStore, Depends(_get_store)],
) -> ClipCandidateCreate:
    """Create one clip candidate (idempotent register)."""
    try:
        record = store.register_candidate(
            media_asset_id=request.media_asset_id,
            org_id=request.org_id,
            start_ms=request.start_ms,
            end_ms=request.end_ms,
            source=request.source.value,
            reason=request.reason,
        )
        return ClipCandidateCreate(
            media_asset_id=record.media_asset_id,
            org_id=record.org_id,
            start_ms=record.start_ms,
            end_ms=record.end_ms,
            source=ClipCandidateSource(record.source),
            reason=record.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/clip-candidates/{candidate_id}",
    response_model=ClipCandidateCreate,
    name="get_clip_candidate",
)
def get_clip_candidate_route(
    store: Annotated[ClipCandidateStore, Depends(_get_store)],
    candidate_id: Annotated[str, Path(min_length=1, max_length=36)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> ClipCandidateCreate:
    """Get one clip candidate by ID and org."""
    record = store.get(candidate_id, org_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"clip candidate not found: {candidate_id}")
    return ClipCandidateCreate(
        media_asset_id=record.media_asset_id,
        org_id=record.org_id,
        start_ms=record.start_ms,
        end_ms=record.end_ms,
        source=ClipCandidateSource(record.source),
        reason=record.reason,
    )


@router.get(
    "/clip-candidates", response_model=list[ClipCandidateCreate], name="list_clip_candidates"
)
def list_clip_candidates_route(
    store: Annotated[ClipCandidateStore, Depends(_get_store)],
    org_id: str = Query(min_length=1, max_length=64),
    status: str | None = Query(default=None),
) -> list[ClipCandidateCreate]:
    """List clip candidates by org and optionally by status."""
    if status:
        records = store.list_by_status(status, org_id)
    else:
        records = store.list_by_org(org_id)
    return [
        ClipCandidateCreate(
            media_asset_id=r.media_asset_id,
            org_id=r.org_id,
            start_ms=r.start_ms,
            end_ms=r.end_ms,
            source=ClipCandidateSource(r.source),
            reason=r.reason,
        )
        for r in records
    ]


@router.post(
    "/clip-candidates/extract-and-register",
    response_model=list[ClipCandidateCreate],
    name="extract_and_register_clip_candidates",
)
def extract_and_register_clip_candidates_route(
    request: ExtractAndRegisterRequest,
    store: Annotated[ClipCandidateStore, Depends(_get_store)],
) -> list[ClipCandidateCreate]:
    """Extract clip candidates from boundaries and register them.

    Runs the pure heuristic extractor (no LLM, no network, no ffmpeg),
    then registers each candidate idempotently.
    """
    # Build params dict from request
    params_dict = {
        "min_len_ms": request.min_len_ms,
        "max_len_ms": request.max_len_ms,
        "min_gap_ms": request.min_gap_ms,
        "source": request.source.value,
        "reason": request.reason,
    }

    # Run extractor + register
    records = extract_and_register(
        store=store,
        media_asset_id=request.media_asset_id,
        org_id=request.org_id,
        boundaries=request.boundaries,
        params=params_dict,
    )

    return [
        ClipCandidateCreate(
            media_asset_id=r.media_asset_id,
            org_id=r.org_id,
            start_ms=r.start_ms,
            end_ms=r.end_ms,
            source=ClipCandidateSource(r.source),
            reason=r.reason,
        )
        for r in records
    ]
