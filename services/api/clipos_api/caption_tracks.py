"""Caption tracks and SRT generation (CLIP-052).

Persistence for the M12 caption layer: a caption track binds validated
timed-text segments to one render project, and `segments_to_srt`
deterministically renders them as SubRip for ffmpeg burn-in (CLIP-051's
argv builder consumes the file). `validate_caption_segments` is the pure
gate: non-empty, <= 200 segments, each 1..200 chars, start < end inside
the render duration, sorted and non-overlapping — deterministic, no
network. Registration is idempotent per `(org_id, track_id)` with a
render-project existence gate (404) and conflict on changed fields (409);
audited `caption_tracks.registered`. No LLM, no provider identifiers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.render_project import RenderProjectRecord
from clipos_api.repository import Base

MAX_SEGMENTS = 200
MAX_SEGMENT_TEXT = 200


class CaptionSegment(BaseModel):
    """One timed-text cue inside a caption track."""

    model_config = ConfigDict(frozen=True)

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=MAX_SEGMENT_TEXT)

    @model_validator(mode="after")
    def _window_order(self) -> CaptionSegment:
        if self.end_ms <= self.start_ms:
            raise ValueError("caption segment end_ms must be greater than start_ms")
        return self


class CaptionTrackRequest(BaseModel):
    """Request body for registering one caption track."""

    model_config = ConfigDict(frozen=True)

    track_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    render_project_id: str = Field(min_length=1, max_length=128)
    segments: list[CaptionSegment] = Field(min_length=1, max_length=MAX_SEGMENTS)
    style_ref: str | None = Field(default=None, max_length=128)
    language: str = Field(default="en", min_length=2, max_length=8)


class CaptionTrack(BaseModel):
    """One persisted caption track."""

    model_config = ConfigDict(frozen=True)

    track_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    render_project_id: str = Field(min_length=1, max_length=128)
    segments: list[CaptionSegment] = Field(default_factory=list)
    style_ref: str | None = Field(default=None, max_length=128)
    language: str = Field(min_length=2, max_length=8)
    created_at: datetime | None = None


def validate_caption_segments(segments: list[CaptionSegment], duration_ms: int) -> list[str]:
    """Pure caption validation against the render duration (errors empty = ok)."""
    errors: list[str] = []
    if not segments:
        return ["segments must be non-empty"]
    if len(segments) > MAX_SEGMENTS:
        errors.append(f"segments must contain at most {MAX_SEGMENTS} entries")
        return errors
    previous_end = -1
    for index, segment in enumerate(segments):
        if segment.end_ms > duration_ms:
            errors.append(f"segments[{index}] exceeds render duration ({duration_ms} ms)")
        if segment.start_ms < previous_end:
            errors.append(f"segments[{index}] overlaps the previous segment")
        previous_end = segment.end_ms
    return errors


def _srt_timestamp(total_ms: int) -> str:
    """HH:MM:SS,mmm SubRip timestamp (pure)."""
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def segments_to_srt(segments: list[CaptionSegment]) -> str:
    """Deterministic SubRip rendering of ordered caption segments (pure)."""
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        blocks.append(
            f"{index}\n"
            f"{_srt_timestamp(segment.start_ms)} --> {_srt_timestamp(segment.end_ms)}\n"
            f"{segment.text}"
        )
    return "\n\n".join(blocks) + "\n"


class CaptionTrackRecord(Base):
    """SQL row for one caption track."""

    __tablename__ = "caption_tracks"
    __table_args__ = (
        sa.Index("uq_caption_tracks_org_track", "org_id", "track_id", unique=True),
        sa.Index("ix_caption_tracks_org_render", "org_id", "render_project_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    render_project_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    segments: Mapped[list[dict]] = mapped_column(sa.JSON, nullable=False)
    style_ref: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    language: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _track_from_row(row: CaptionTrackRecord) -> CaptionTrack:
    segments = [CaptionSegment(**segment) for segment in (row.segments or [])]
    return CaptionTrack(
        track_id=row.track_id,
        org_id=row.org_id,
        render_project_id=row.render_project_id,
        segments=segments,
        style_ref=row.style_ref,
        language=row.language,
        created_at=_aware(row.created_at),
    )


class CaptionTrackStore:
    """Typed persistence for caption tracks."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> CaptionTrackStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_track(self, request: CaptionTrackRequest) -> CaptionTrack:
        """Validate segments against the project duration; idempotent register."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            project = session.scalars(
                sa.select(RenderProjectRecord).where(
                    RenderProjectRecord.render_project_id == request.render_project_id,
                    RenderProjectRecord.org_id == request.org_id,
                )
            ).first()
            if project is None:
                raise ValueError(f"render project not found: {request.render_project_id}")
            spec = dict(project.spec or {})
            composition = spec.get("composition", {})
            duration_ms = composition.get("duration_ms")
            if not isinstance(duration_ms, int) or duration_ms <= 0:
                raise ValueError("render project spec missing valid duration_ms")
            errors = validate_caption_segments(request.segments, duration_ms)
            if errors:
                raise ValueError("invalid caption track: " + "; ".join(errors[:5]))
            existing = session.scalars(
                sa.select(CaptionTrackRecord).where(
                    CaptionTrackRecord.org_id == request.org_id,
                    CaptionTrackRecord.track_id == request.track_id,
                )
            ).first()
            if existing is not None:
                stored = _track_from_row(existing)
                if (
                    stored.render_project_id != request.render_project_id
                    or stored.segments != list(request.segments)
                    or stored.style_ref != request.style_ref
                    or stored.language != request.language
                ):
                    raise ValueError(
                        f"caption track already registered with different fields: "
                        f"{request.track_id}"
                    )
                return stored
            row = CaptionTrackRecord(
                track_id=request.track_id,
                org_id=request.org_id,
                render_project_id=request.render_project_id,
                segments=[segment.model_dump() for segment in request.segments],
                style_ref=request.style_ref,
                language=request.language,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "caption_tracks.registered",
                "caption_tracks",
                f"caption_track:{request.track_id}",
                {
                    "org_id": request.org_id,
                    "render_project_id": request.render_project_id,
                    "segment_count": len(request.segments),
                    "language": request.language,
                },
            )
            return _track_from_row(row)

    def get_track(self, track_id: str, org_id: str) -> CaptionTrack | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(CaptionTrackRecord).where(
                    CaptionTrackRecord.track_id == track_id,
                    CaptionTrackRecord.org_id == org_id,
                )
            ).first()
            return _track_from_row(row) if row is not None else None

    def list_tracks(self, org_id: str, render_project_id: str) -> list[CaptionTrack]:
        with self._sessions() as session:
            project = session.scalars(
                sa.select(RenderProjectRecord).where(
                    RenderProjectRecord.render_project_id == render_project_id,
                    RenderProjectRecord.org_id == org_id,
                )
            ).first()
            if project is None:
                raise ValueError(f"render project not found: {render_project_id}")
            rows = session.scalars(
                sa.select(CaptionTrackRecord)
                .where(
                    CaptionTrackRecord.org_id == org_id,
                    CaptionTrackRecord.render_project_id == render_project_id,
                )
                .order_by(CaptionTrackRecord.created_at, CaptionTrackRecord.id)
            ).all()
            return [_track_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["caption-tracks"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> CaptionTrackStore:
    return CaptionTrackStore.from_engine(engine)


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
    "/render-projects/{render_project_id}/caption-tracks",
    response_model=CaptionTrack,
    name="register_caption_track",
)
def register_caption_track_route(
    request: CaptionTrackRequest,
    render_project_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[CaptionTrackStore, Depends(_store)],
) -> CaptionTrack:
    """Register one validated caption track for a render project."""
    if request.render_project_id != render_project_id:
        raise HTTPException(
            status_code=422,
            detail="track render_project_id must match the path render_project_id",
        )
    try:
        return store.register_track(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/render-projects/{render_project_id}/caption-tracks",
    response_model=list[CaptionTrack],
    name="list_caption_tracks",
)
def list_caption_tracks_route(
    render_project_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[CaptionTrackStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[CaptionTrack]:
    """List caption tracks for one render project (404 when unknown)."""
    try:
        return store.list_tracks(org_id, render_project_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/caption-tracks/{track_id}",
    response_model=CaptionTrack,
    name="get_caption_track",
)
def get_caption_track_route(
    track_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[CaptionTrackStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> CaptionTrack:
    """Get one caption track by id and org."""
    stored = store.get_track(track_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"caption track not found: {track_id}")
    return stored
