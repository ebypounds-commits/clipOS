"""Duplicate and content fingerprint checks for clip candidates (CLIP-044).

Deterministic dedup over clip candidates with two independent signals:

- **window overlap**: candidates on the same media asset whose time windows
  overlap by at least ``threshold`` of the shorter window are duplicates;
- **transcript content**: a sha256 fingerprint over the normalized transcript
  text inside the candidate window catches the same spoken content reused at
  different timestamps or in different assets.

Fingerprints are persisted facts (``candidate_fingerprints``, append-only per
``(org, candidate, kind)``); duplicate relations are derived on demand by
comparing other candidates' windows and transcript text directly, so a check
never depends on the corpus having been fingerprinted first and a policy
threshold change never invalidates stored data. Manifest-level hashes from
CLIP-032 (``content_fingerprints``) are a separate provenance concern and are
not duplicated here. No LLM, no network, no provider identifiers.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.clip_candidates import ClipCandidateRecord
from clipos_api.repository import Base


class FingerprintKind(str, Enum):
    """What a candidate fingerprint identifies."""

    WINDOW = "WINDOW"
    TRANSCRIPT = "TRANSCRIPT"


# ---------------------------------------------------------------------------
# Pure deterministic helpers (no LLM, no network, no DB)
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """Lowercase and collapse whitespace runs to single spaces (pure)."""
    return " ".join(text.lower().split())


def overlap_over_shorter(a_start_ms: int, a_end_ms: int, b_start_ms: int, b_end_ms: int) -> float:
    """Overlap of two windows divided by the shorter window length (pure).

    Returns 0..1 rounded to 4 decimals; 0.0 for degenerate or disjoint
    windows. A window fully containing another scores 1.0 (the shorter
    window is completely covered).
    """
    if a_end_ms <= a_start_ms or b_end_ms <= b_start_ms:
        return 0.0
    overlap = max(0, min(a_end_ms, b_end_ms) - max(a_start_ms, b_start_ms))
    shorter = min(a_end_ms - a_start_ms, b_end_ms - b_start_ms)
    if shorter <= 0:
        return 0.0
    return round(overlap / shorter, 4)


def window_fingerprint(media_asset_id: str, start_ms: int, end_ms: int) -> str:
    """sha256 of the canonical window identity (pure)."""
    payload = f"window:{media_asset_id}:{start_ms}-{end_ms}"
    return hashlib.sha256(payload.encode()).hexdigest()


def transcript_fingerprint(text: str) -> str | None:
    """sha256 of the normalized transcript text (pure).

    Returns None when the normalized text is empty — an absent transcript is
    not content evidence and must never collide across candidates.
    """
    normalized = normalize_text(text)
    if not normalized:
        return None
    return hashlib.sha256(f"transcript:{normalized}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Typed contracts
# ---------------------------------------------------------------------------


class CandidateFingerprint(BaseModel):
    """One persisted fingerprint fact for one candidate."""

    model_config = ConfigDict(frozen=True)

    fingerprint_id: str = Field(
        default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128
    )
    org_id: str = Field(min_length=1, max_length=64)
    candidate_id: str = Field(min_length=1, max_length=36)
    media_asset_id: str = Field(min_length=1, max_length=128)
    kind: FingerprintKind
    algorithm: Literal["sha256"] = "sha256"
    value: str = Field(min_length=64, max_length=64)
    created_at: datetime | None = None


class DuplicateCheckRequest(BaseModel):
    """Inputs for one duplicate-check run over one candidate."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    threshold: float = Field(default=0.5, gt=0.0, le=1.0)


class DuplicateFinding(BaseModel):
    """One colliding candidate returned by a duplicate check."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1, max_length=36)
    media_asset_id: str = Field(min_length=1, max_length=128)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class DuplicateCheckReport(BaseModel):
    """Result of one duplicate-check run: persisted facts + derived findings."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1, max_length=36)
    org_id: str = Field(min_length=1, max_length=64)
    threshold: float
    fingerprints: list[CandidateFingerprint] = Field(default_factory=list)
    window_duplicates: list[DuplicateFinding] = Field(default_factory=list)
    content_duplicates: list[DuplicateFinding] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SQL row
# ---------------------------------------------------------------------------


class CandidateFingerprintRecord(Base):
    """SQL row for one persisted candidate fingerprint."""

    __tablename__ = "candidate_fingerprints"
    __table_args__ = (
        sa.Index(
            "uq_candidate_fingerprints_org_candidate_kind",
            "org_id",
            "candidate_id",
            "kind",
            unique=True,
        ),
        sa.Index("ix_candidate_fingerprints_org_kind_value", "org_id", "kind", "value"),
        sa.Index("ix_candidate_fingerprints_org", "org_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    fingerprint_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    candidate_id: Mapped[str] = mapped_column(sa.String(36), nullable=False)
    media_asset_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    algorithm: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    value: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _fingerprint_from_row(row: CandidateFingerprintRecord) -> CandidateFingerprint:
    return CandidateFingerprint(
        fingerprint_id=row.fingerprint_id,
        org_id=row.org_id,
        candidate_id=row.candidate_id,
        media_asset_id=row.media_asset_id,
        kind=FingerprintKind(row.kind),
        algorithm=cast(Literal["sha256"], row.algorithm),
        value=row.value,
        created_at=_aware(row.created_at),
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ClipDedupStore:
    """Typed persistence for candidate fingerprints plus dedup context reads."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ClipDedupStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def load_candidate(self, candidate_id: str, org_id: str) -> ClipCandidateRecord:
        """Load one candidate, org-scoped; missing raises ValueError (404)."""
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ClipCandidateRecord).where(
                    ClipCandidateRecord.candidate_id == candidate_id,
                    ClipCandidateRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"clip candidate not found: {candidate_id}")
            return row

    def persist_fingerprint(self, fingerprint: CandidateFingerprint) -> CandidateFingerprint:
        """Persist one fingerprint idempotently per (org, candidate, kind).

        Identical re-persist returns the stored row; a different value for the
        same key raises (the underlying content must never silently change).
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(CandidateFingerprintRecord).where(
                    CandidateFingerprintRecord.org_id == fingerprint.org_id,
                    CandidateFingerprintRecord.candidate_id == fingerprint.candidate_id,
                    CandidateFingerprintRecord.kind == fingerprint.kind.value,
                )
            ).first()
            if existing is not None:
                stored = _fingerprint_from_row(existing)
                if (
                    stored.value == fingerprint.value
                    and stored.algorithm == fingerprint.algorithm
                    and stored.media_asset_id == fingerprint.media_asset_id
                ):
                    return stored
                raise ValueError(
                    f"candidate already fingerprinted with different value: "
                    f"{fingerprint.candidate_id} kind={fingerprint.kind.value}"
                )
            row = CandidateFingerprintRecord(
                fingerprint_id=fingerprint.fingerprint_id,
                org_id=fingerprint.org_id,
                candidate_id=fingerprint.candidate_id,
                media_asset_id=fingerprint.media_asset_id,
                kind=fingerprint.kind.value,
                algorithm=fingerprint.algorithm,
                value=fingerprint.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "candidate_fingerprints.persisted",
                "candidate_fingerprints",
                f"fingerprint:{row.fingerprint_id}",
                {
                    "org_id": fingerprint.org_id,
                    "candidate_id": fingerprint.candidate_id,
                    "kind": fingerprint.kind.value,
                    "algorithm": fingerprint.algorithm,
                    "value_prefix": fingerprint.value[:12],
                },
            )
            return _fingerprint_from_row(row)

    def list_fingerprints(self, candidate_id: str, org_id: str) -> list[CandidateFingerprint]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(CandidateFingerprintRecord)
                .where(
                    CandidateFingerprintRecord.org_id == org_id,
                    CandidateFingerprintRecord.candidate_id == candidate_id,
                )
                .order_by(CandidateFingerprintRecord.kind)
            ).all()
            return [_fingerprint_from_row(row) for row in rows]

    def list_all_other_candidates(
        self, org_id: str, exclude_candidate_id: str
    ) -> list[ClipCandidateRecord]:
        """Every other candidate in the org (window and content comparison)."""
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ClipCandidateRecord).where(
                    ClipCandidateRecord.org_id == org_id,
                    ClipCandidateRecord.candidate_id != exclude_candidate_id,
                )
            ).all()
            return list(rows)


# ---------------------------------------------------------------------------
# Duplicate-check flow
# ---------------------------------------------------------------------------


def _window_text(
    store: ClipDedupStore, org_id: str, media_asset_id: str, start_ms: int, end_ms: int
) -> str:
    """Ordered transcript text overlapping the window from the primary transcript.

    Primary transcript = earliest (created_at, transcript_id) for the asset —
    a deterministic pick that keeps language mixing out of content hashes.
    Segments are ordered by seq so the hash is byte-stable.
    """
    # Deferred import: transcription_store <-> transcription cycle (CLIP-043).
    from clipos_api.transcription_store import SegmentRecord, TranscriptRecord

    with store._sessions() as session:
        transcript = session.scalars(
            sa.select(TranscriptRecord)
            .where(
                TranscriptRecord.org_id == org_id,
                TranscriptRecord.media_asset_id == media_asset_id,
            )
            .order_by(TranscriptRecord.created_at, TranscriptRecord.transcript_id)
        ).first()
        if transcript is None:
            return ""
        segments = session.scalars(
            sa.select(SegmentRecord)
            .where(
                SegmentRecord.transcript_id == transcript.transcript_id,
                SegmentRecord.org_id == org_id,
                SegmentRecord.end_ms > start_ms,
                SegmentRecord.start_ms < end_ms,
            )
            .order_by(SegmentRecord.seq)
        ).all()
        return " ".join(segment.text for segment in segments)


def check_candidate_duplicates(
    store: ClipDedupStore,
    candidate_id: str,
    request: DuplicateCheckRequest,
) -> DuplicateCheckReport:
    """Persist fingerprints and derive duplicate findings for one candidate.

    Always persists the WINDOW fingerprint; persists the TRANSCRIPT
    fingerprint only when non-empty text exists in the window. Window
    duplicates require overlap >= threshold of the shorter window on the
    same asset; content duplicates hash the transcript text of every other
    candidate's window in the org and compare directly — no prior
    fingerprinting of the corpus is required. Raises ValueError with a
    message the route maps to 404/409/422.
    """
    if not (0.0 < request.threshold <= 1.0):
        raise ValueError("threshold must be in (0..1]")
    candidate = store.load_candidate(candidate_id, request.org_id)

    fingerprints: list[CandidateFingerprint] = []
    fingerprints.append(
        store.persist_fingerprint(
            CandidateFingerprint(
                org_id=request.org_id,
                candidate_id=candidate_id,
                media_asset_id=candidate.media_asset_id,
                kind=FingerprintKind.WINDOW,
                value=window_fingerprint(
                    candidate.media_asset_id, candidate.start_ms, candidate.end_ms
                ),
            )
        )
    )

    window_text = _window_text(
        store, request.org_id, candidate.media_asset_id, candidate.start_ms, candidate.end_ms
    )
    content_hash = transcript_fingerprint(window_text)
    if content_hash is not None:
        fingerprints.append(
            store.persist_fingerprint(
                CandidateFingerprint(
                    org_id=request.org_id,
                    candidate_id=candidate_id,
                    media_asset_id=candidate.media_asset_id,
                    kind=FingerprintKind.TRANSCRIPT,
                    value=content_hash,
                )
            )
        )

    window_duplicates: list[DuplicateFinding] = []
    content_duplicates: list[DuplicateFinding] = []
    for other in store.list_all_other_candidates(request.org_id, candidate_id):
        if other.media_asset_id == candidate.media_asset_id and (
            overlap_over_shorter(candidate.start_ms, candidate.end_ms, other.start_ms, other.end_ms)
            >= request.threshold
        ):
            window_duplicates.append(
                DuplicateFinding(
                    candidate_id=other.candidate_id,
                    media_asset_id=other.media_asset_id,
                    start_ms=other.start_ms,
                    end_ms=other.end_ms,
                )
            )
        if content_hash is not None:
            other_text = _window_text(
                store,
                request.org_id,
                other.media_asset_id,
                other.start_ms,
                other.end_ms,
            )
            if transcript_fingerprint(other_text) == content_hash:
                content_duplicates.append(
                    DuplicateFinding(
                        candidate_id=other.candidate_id,
                        media_asset_id=other.media_asset_id,
                        start_ms=other.start_ms,
                        end_ms=other.end_ms,
                    )
                )

    audit.record(
        "clip_candidates.duplicate_check",
        "clip_candidates",
        f"candidate:{candidate_id}",
        {
            "org_id": request.org_id,
            "threshold": request.threshold,
            "window_duplicate_count": len(window_duplicates),
            "content_duplicate_count": len(content_duplicates),
            "fingerprint_kinds": sorted(fp.kind.value for fp in fingerprints),
        },
    )
    return DuplicateCheckReport(
        candidate_id=candidate_id,
        org_id=request.org_id,
        threshold=request.threshold,
        fingerprints=fingerprints,
        window_duplicates=window_duplicates,
        content_duplicates=content_duplicates,
    )


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["clip-dedup"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ClipDedupStore:
    return ClipDedupStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


@router.post(
    "/clip-candidates/{candidate_id}/duplicate-check",
    response_model=DuplicateCheckReport,
    name="check_clip_duplicates",
)
def check_clip_duplicates_route(
    request: DuplicateCheckRequest,
    candidate_id: Annotated[str, Path(min_length=1, max_length=36)],
    store: Annotated[ClipDedupStore, Depends(_store)],
) -> DuplicateCheckReport:
    """Persist fingerprints and return derived duplicate findings."""
    try:
        return check_candidate_duplicates(store, candidate_id, request)
    except ValueError as exc:
        message = str(exc)
        if "not found" in message:
            status = 404
        elif "already fingerprinted with different" in message:
            status = 409
        else:
            status = 422
        raise HTTPException(status_code=status, detail=message) from exc


@router.get(
    "/clip-candidates/{candidate_id}/fingerprints",
    response_model=list[CandidateFingerprint],
    name="list_candidate_fingerprints",
)
def list_candidate_fingerprints_route(
    candidate_id: Annotated[str, Path(min_length=1, max_length=36)],
    store: Annotated[ClipDedupStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[CandidateFingerprint]:
    """List persisted fingerprints for one candidate (404 when unknown)."""
    try:
        store.load_candidate(candidate_id, org_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return store.list_fingerprints(candidate_id, org_id)
