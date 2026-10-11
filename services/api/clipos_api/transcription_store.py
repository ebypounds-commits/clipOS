"""TranscriptionStore with SQLAlchemy backend.

Provides from_engine classmethod, register_transcript (idempotent),
record_segments (append-only, seq monotonic), get_transcript, and
list_segments. Uses the repository Base/pattern from clipos_api.repository.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import DateTime, Index, Integer, String, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from clipos_api.repository import Base, session_scope
from clipos_api.transcription import Segment

# ---------------------------------------------------------------------------
# Declarative model for transcripts
# ---------------------------------------------------------------------------


class TranscriptRecord(Base):
    """SQLAlchemy declarative model for transcripts."""

    __tablename__ = "transcripts"

    transcript_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    media_asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    provider_alias: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    model_hint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint(
            "org_id", "media_asset_id", "language", name="uq_transcripts_org_media_lang"
        ),
        Index("ix_transcripts_status", "status"),
    )

    @staticmethod
    def _contract(record) -> dict:
        from datetime import UTC as _UTC

        def _aware(value):
            if value is None:
                return None
            if value.tzinfo is None:
                return value.replace(tzinfo=_UTC)
            return value

        return {
            "transcript_id": record.transcript_id,
            "media_asset_id": record.media_asset_id,
            "org_id": record.org_id,
            "language": record.language,
            "provider_alias": record.provider_alias,
            "status": record.status,
            "model_hint": record.model_hint,
            "created_at": _aware(record.created_at),
            "updated_at": _aware(record.updated_at),
        }


class SegmentRecord(Base):
    """Declarative row for one transcript segment (append-only)."""

    __tablename__ = "transcript_segments"

    segment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    transcript_id: Mapped[str] = mapped_column(String(36), nullable=False)
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(String(2048), nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_transcript_segments_transcript_seq", "transcript_id", "seq", unique=True),
    )


# ---------------------------------------------------------------------------
# TranscriptionStore
# ---------------------------------------------------------------------------


class TranscriptionStore:
    """SQL-backed transcript store conforming to the store protocol.

    Notes
    -----
    - register_transcript is idempotent: (org_id, media_asset_id, language)
      unique constraint means conflicts raise IntegrityError.
    - record_segments is append-only: seq must be strictly monotonic per
      transcript (>= 0); conflicts raise IntegrityError.
    - get_transcript / list_segments return None / [] when not found.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # from_engine
    # ------------------------------------------------------------------

    @classmethod
    def from_engine(cls, engine: Engine) -> TranscriptionStore:
        """Factory returning a TranscriptionStore bound to *engine*."""
        return cls(engine)

    # ------------------------------------------------------------------
    # register_transcript — idempotent per (org_id, media_asset_id, language)
    # ------------------------------------------------------------------

    def register_transcript(
        self,
        media_asset_id: str,
        org_id: str,
        language: str,
        provider_alias: str,
        model_hint: str | None = None,
    ) -> dict:
        """Register a transcript; idempotent per (org_id, media_asset_id, language).

        Returns the transcript dict on success.  Raises IntegrityError on
        conflict (duplicate org/media/lang).
        """
        transcript_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        with session_scope(self._engine) as session:
            # Check if already exists (idempotent: return existing)
            existing = session.scalar(
                select(TranscriptRecord).where(
                    TranscriptRecord.org_id == org_id,
                    TranscriptRecord.media_asset_id == media_asset_id,
                    TranscriptRecord.language == language,
                )
            )
            if existing is not None:
                return TranscriptRecord._contract(existing)

            record = TranscriptRecord(
                transcript_id=transcript_id,
                media_asset_id=media_asset_id,
                org_id=org_id,
                language=language,
                provider_alias=provider_alias,
                status="PENDING",
                model_hint=model_hint,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            session.flush()
            return TranscriptRecord._contract(record)

    # ------------------------------------------------------------------
    # record_segments — append-only, seq monotonic per transcript
    # ------------------------------------------------------------------

    def record_segments(
        self,
        transcript_id: str,
        org_id: str,
        segments: Sequence[Segment],
    ) -> list[dict]:
        """Append segments for a transcript.

        seq must be strictly monotonic (>= 0) per transcript; conflicts raise
        IntegrityError.  All segments are persisted and the full list is
        returned as dicts.
        """

        with session_scope(self._engine) as session:
            # Verify transcript exists and belongs to org
            transcript = session.get(TranscriptRecord, transcript_id)
            if transcript is None:
                raise ValueError(f"Transcript {transcript_id} not found")
            if transcript.org_id != org_id:
                raise ValueError("Transcript org_id mismatch")

            # Check existing max seq for this transcript
            from sqlalchemy import text

            result = session.execute(
                text(
                    "SELECT COALESCE(MAX(seq), -1) FROM transcript_segments "
                    "WHERE transcript_id = :tid"
                ).bindparams(tid=transcript_id)
            ).scalar()

            max_seq = result if result is not None else -1

            # Existing rows for idempotent skip check
            existing_rows = {
                row.seq: row
                for row in session.scalars(
                    select(SegmentRecord).where(SegmentRecord.transcript_id == transcript_id)
                ).all()
            }

            # Validate monotonicity for NEW segments only; identical re-appends are no-ops
            new_segments = []
            for seg in segments:
                prior = existing_rows.get(seg.seq)
                if prior is not None:
                    if (
                        prior.text != seg.text
                        or prior.start_ms != seg.start_ms
                        or prior.end_ms != seg.end_ms
                        or prior.speaker != seg.speaker
                    ):
                        raise ValueError(
                            f"Segment seq {seg.seq} already recorded with different fields"
                        )
                    continue  # idempotent no-op
                if seg.seq <= max_seq:
                    raise ValueError(f"Segment seq {seg.seq} must be > max existing seq {max_seq}")
                new_segments.append(seg)
                max_seq = seg.seq
            segments = new_segments

            # Persist segments
            for seg in segments:
                seg_id = str(uuid.uuid4())[:12]
                session.add(
                    SegmentRecord(
                        segment_id=seg_id,
                        transcript_id=transcript_id,
                        org_id=org_id,
                        seq=seg.seq,
                        start_ms=seg.start_ms,
                        end_ms=seg.end_ms,
                        text=seg.text,
                        speaker=seg.speaker,
                    )
                )

            # Audit event
            from clipos_api.audit import record as audit_record

            audit_record(
                "transcription.segments_appended",
                "transcription-store",
                transcript_id,
                {
                    "org_id": org_id,
                    "segment_count": len(segments),
                    "transcript_id": transcript_id,
                },
            )

            # Return persisted segments as dicts
            output: list[dict] = []
            for seg in segments:
                output.append(
                    {
                        "segment_id": seg.segment_id if hasattr(seg, "segment_id") else seg_id,
                        "transcript_id": transcript_id,
                        "org_id": org_id,
                        "seq": seg.seq,
                        "start_ms": seg.start_ms,
                        "end_ms": seg.end_ms,
                        "text": seg.text,
                        "speaker": seg.speaker,
                    }
                )
            return output

    # ------------------------------------------------------------------
    # get_transcript
    # ------------------------------------------------------------------

    def get_transcript(self, transcript_id: str, org_id: str) -> dict | None:
        """Return the transcript dict, scoped to org_id, or None."""
        with session_scope(self._engine) as session:
            record = session.get(TranscriptRecord, transcript_id)
            if record is None or record.org_id != org_id:
                return None
            return TranscriptRecord._contract(record)

    # ------------------------------------------------------------------
    # list_segments
    # ------------------------------------------------------------------

    def list_segments(self, transcript_id: str, org_id: str, limit: int = 100) -> list[dict]:
        """Return segments for a transcript, scoped to org_id."""
        from sqlalchemy import text

        with session_scope(self._engine) as session:
            # First verify transcript org access
            transcript = session.get(TranscriptRecord, transcript_id)
            if transcript is None or transcript.org_id != org_id:
                return []

            rows = (
                session.execute(
                    text(
                        "SELECT segment_id, transcript_id, org_id, seq, "
                        "start_ms, end_ms, text, speaker "
                        "FROM transcript_segments "
                        "WHERE transcript_id = :tid "
                        "ORDER BY seq ASC LIMIT :lim"
                    ).bindparams(tid=transcript_id, lim=limit)
                )
                .mappings()
                .all()
            )

            result: list[dict] = []
            for row in rows:
                result.append(
                    {
                        "segment_id": row["segment_id"],
                        "transcript_id": row["transcript_id"],
                        "org_id": row["org_id"],
                        "seq": row["seq"],
                        "start_ms": row["start_ms"],
                        "end_ms": row["end_ms"],
                        "text": row["text"],
                        "speaker": row["speaker"],
                    }
                )
            return result
