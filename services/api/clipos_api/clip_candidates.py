"""Clip candidate data model and heuristic extractor (CLIP-042)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base


class ClipCandidateStatus(str, Enum):
    """Lifecycle status of one clip candidate."""

    NEW = "NEW"
    SCORED = "SCORED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ClipCandidateReason(str, Enum):
    """Reason a clip candidate was created."""

    SEMANTIC = "SEMANTIC"
    MANUAL = "MANUAL"
    HEURISTIC = "HEURISTIC"


class ClipCandidateSource(str, Enum):
    """Source of the clip candidate."""

    SEMANTIC = "SEMANTIC"
    MANUAL = "MANUAL"
    HEURISTIC = "HEURISTIC"


#: SQL row for one clip candidate
class ClipCandidateRecord(Base):
    """SQL row for one clip candidate."""

    __tablename__ = "clip_candidates"
    __table_args__ = (
        Index(
            "uq_clip_candidates_org_id_candidate_id",
            "org_id",
            "candidate_id",
            unique=True,
        ),
        Index("ix_clip_candidates_org_id", "org_id"),
        Index("ix_clip_candidates_status", "status"),
    )

    candidate_id: Mapped[str] = mapped_column(String(36), primary_key=True, unique=True)
    media_asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="NEW")
    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def extract_candidates(
    boundaries: list[tuple[int, int]],
    min_len_ms: int,
    max_len_ms: int,
    min_gap_ms: int,
) -> list[tuple[int, int]]:
    """Pure heuristic extractor: clip segments to length window, merge overlap,
    respect gap. NO LLM, NO network, NO ffmpeg calls.

    Deterministic — takes semantic segment boundaries, clips to length window,
    merges overlapping, respects gap.

    Args:
        boundaries: list of (start_ms, end_ms) semantic segment boundaries
        min_len_ms: minimum clip length in ms (inclusive)
        max_len_ms: maximum clip length in ms (inclusive)
        min_gap_ms: minimum gap between clips in ms.

    Returns:
        list of (start_ms, end_ms) clipped, merged, gap-respected clips
    """
    if not boundaries:
        return []

    # Normalize and sort boundaries by start time
    sorted_boundaries = sorted(boundaries, key=lambda b: b[0])

    # Clip each boundary to the length window
    clipped: list[tuple[int, int]] = []
    for start, end in sorted_boundaries:
        length = end - start
        if length < min_len_ms:
            # Too short, skip
            continue
        if length > max_len_ms:
            # Trim to max length - take the first max_len_ms portion
            end = start + max_len_ms
        clipped.append((start, end))

    if not clipped:
        return []

    # Merge overlapping/clipped segments respecting min_gap_ms.
    # The gap between two clips is current_start - last_end.
    # Merge behavior:
    #   - Overlapping clips (gap < 0): always merge
    #   - Touching clips (gap == 0): merge only if min_gap_ms == 0
    #   - Positive gap: merge only if gap < min_gap_ms (strictly less than)
    #   - gap >= min_gap_ms: keep separate
    merged: list[tuple[int, int]] = [clipped[0]]
    for current_start, current_end in clipped[1:]:
        last_start, last_end = merged[-1]
        gap = current_start - last_end
        if gap < 0:
            # Overlapping clips, always merge
            merged[-1] = (last_start, max(last_end, current_end))
        elif gap == 0 and min_gap_ms == 0:
            # Touching clips with min_gap=0, merge
            merged[-1] = (last_start, max(last_end, current_end))
        elif 0 < gap < min_gap_ms:
            # Positive gap less than min_gap, merge
            merged[-1] = (last_start, max(last_end, current_end))
        else:
            # gap >= min_gap_ms or (gap == 0 and min_gap_ms > 0): keep separate
            merged.append((current_start, current_end))

    return merged


def extract_and_register(
    store: ClipCandidateStore,
    media_asset_id: str,
    org_id: str,
    boundaries: list[tuple[int, int]],
    params: dict[str, Any],
) -> list[ClipCandidateRecord]:
    """Run heuristic extractor, register each candidate, audit
    clips.candidate_registered once per candidate.

    Args:
        store: ClipCandidateStore instance
        media_asset_id: media asset identifier
        org_id: organization identifier
        boundaries: segment boundaries [(start_ms, end_ms), ...]
        params: dict with min_len_ms, max_len_ms, min_gap_ms, source, reason

    Returns:
        list of registered ClipCandidateRecord instances
    """
    min_len_ms = params.get("min_len_ms", 1000)
    max_len_ms = params.get("max_len_ms", 60000)
    min_gap_ms = params.get("min_gap_ms", 0)
    source = params.get("source", ClipCandidateSource.SEMANTIC.value)
    reason = params.get("reason", "heuristic extraction")

    # Run pure heuristic extraction
    clipped = extract_candidates(boundaries, min_len_ms, max_len_ms, min_gap_ms)

    registered: list[ClipCandidateRecord] = []
    for start_ms, end_ms in clipped:
        try:
            record = store.register_candidate(
                media_asset_id=media_asset_id,
                org_id=org_id,
                start_ms=start_ms,
                end_ms=end_ms,
                source=source,
                reason=reason,
            )
            registered.append(record)
        except ValueError:
            # Conflict - skip this candidate (idempotent, already registered)
            pass

    return registered


class ClipCandidateStore:
    """Declarative rows on Base, from_engine: register_candidate (idempotent),
    get, list by org/status."""

    def __init__(self, sessions) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ClipCandidateStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # ------------------------------------------------------------------
    # Register (idempotent: conflicts raise on real change, silent on repeat)
    # ------------------------------------------------------------------

    def register_candidate(
        self,
        media_asset_id: str,
        org_id: str,
        start_ms: int,
        end_ms: int,
        source: str,
        reason: str,
    ) -> ClipCandidateRecord:
        """Idempotent register: returns existing row if identical; raises on
        conflict (different fields for same candidate_id/org_id)."""
        if start_ms < 0:
            raise ValueError(f"start_ms must be >= 0, got {start_ms}")
        if end_ms <= start_ms:
            raise ValueError(
                f"end_ms must be > start_ms, got end_ms={end_ms} <= start_ms={start_ms}"
            )
        if source not in ClipCandidateSource.__members__:
            raise ValueError(f"invalid source: {source}")
        if len(reason) > 255:
            raise ValueError(f"reason must be max 255 chars, got {len(reason)}")

        with self._sessions.begin() as session:
            # Try to find existing candidate by (media_asset_id, org_id, start_ms, end_ms)
            existing = session.scalars(
                sa.select(ClipCandidateRecord).where(
                    ClipCandidateRecord.media_asset_id == media_asset_id,
                    ClipCandidateRecord.org_id == org_id,
                    ClipCandidateRecord.start_ms == start_ms,
                    ClipCandidateRecord.end_ms == end_ms,
                )
            ).first()

            if existing is not None:
                # Check if fields are identical (idempotent repeat)
                if (
                    existing.source == source
                    and existing.reason == reason
                    and existing.status == ClipCandidateStatus.NEW.value
                ):
                    # Truly identical - idempotent, return existing
                    return existing
                else:
                    raise ValueError(
                        f"clip candidate already registered with different fields: "
                        f"{media_asset_id} start={start_ms} end={end_ms}"
                    )

            # Create new candidate
            candidate_id = str(uuid4())[:36]  # uuid4 prefix truncated to 36
            now = datetime.now(UTC)
            row = ClipCandidateRecord(
                candidate_id=candidate_id,
                media_asset_id=media_asset_id,
                org_id=org_id,
                start_ms=start_ms,
                end_ms=end_ms,
                source=source,
                reason=reason,
                status=ClipCandidateStatus.NEW.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "clip_candidate.registered",
                "clip_candidates",
                f"candidate:{candidate_id}",
                {
                    "media_asset_id": media_asset_id,
                    "org_id": org_id,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "source": source,
                },
            )
            return row

    def get(self, candidate_id: str, org_id: str) -> ClipCandidateRecord | None:
        """Get one candidate by ID and org."""
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ClipCandidateRecord).where(
                    ClipCandidateRecord.candidate_id == candidate_id,
                    ClipCandidateRecord.org_id == org_id,
                )
            ).first()
            return row

    def list_by_org(self, org_id: str) -> list[ClipCandidateRecord]:
        """List all candidates for an org."""
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ClipCandidateRecord)
                .where(ClipCandidateRecord.org_id == org_id)
                .order_by(ClipCandidateRecord.candidate_id)
            ).all()
            return list(rows)

    def list_by_status(self, status: str, org_id: str | None = None) -> list[ClipCandidateRecord]:
        """List candidates by status, optionally filtered by org."""
        with self._sessions() as session:
            stmt = sa.select(ClipCandidateRecord).where(ClipCandidateRecord.status == status)
            if org_id is not None:
                stmt = stmt.where(ClipCandidateRecord.org_id == org_id)
            rows = session.scalars(stmt.order_by(ClipCandidateRecord.candidate_id)).all()
            return list(rows)
