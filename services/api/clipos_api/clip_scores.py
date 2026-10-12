"""Multi-factor clip candidate scoring (CLIP-043).

Pure deterministic scoring: four factor scorers compute components in 0..1
from the candidate window, semantic segments, and transcript text;
``score_clip`` combines caller-supplied weights into a 0..100 total. No LLM,
no network, no provider identifiers. Re-scoring a candidate with identical
inputs is an idempotent no-op; different inputs raise. Decided candidates
(APPROVED/REJECTED) are frozen and reject scoring attempts.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.clip_candidates import ClipCandidateRecord, ClipCandidateStatus
from clipos_api.repository import Base
from clipos_api.semantic import SemanticSegmentRecord

#: Factor keys produced by :func:`compute_components`.
COMPONENT_FACTORS = ("length_fit", "hook_coverage", "claim_coverage", "transcript_density")


# ---------------------------------------------------------------------------
# Pure deterministic scorers (no LLM, no network, no DB)
# ---------------------------------------------------------------------------


def length_fit(duration_ms: int, min_len_ms: int, max_len_ms: int) -> float:
    """Fraction of the ideal length window the duration fits (pure).

    Returns 1.0 inside ``[min_len_ms, max_len_ms]``; linear falloff toward 0
    outside the window; 0.0 for non-positive durations or invalid windows.
    """
    if duration_ms <= 0 or min_len_ms < 0 or max_len_ms < min_len_ms:
        return 0.0
    if min_len_ms <= duration_ms <= max_len_ms:
        return 1.0
    if duration_ms < min_len_ms:
        return round(duration_ms / min_len_ms, 4)
    return round(max_len_ms / duration_ms, 4)


def span_coverage(start_ms: int, end_ms: int, spans: list[tuple[int, int]]) -> float:
    """Fraction of ``[start_ms, end_ms)`` covered by the union of spans (pure).

    Overlapping spans are merged before measuring so double-covered regions
    never count twice. Empty, inverted, or disjoint spans score 0.0.
    """
    if end_ms <= start_ms or not spans:
        return 0.0
    clipped = []
    for span_start, span_end in spans:
        left = max(span_start, start_ms)
        right = min(span_end, end_ms)
        if right > left:
            clipped.append((left, right))
    if not clipped:
        return 0.0
    clipped.sort()
    merged: list[list[int]] = [list(clipped[0])]
    for left, right in clipped[1:]:
        if left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    covered = sum(right - left for left, right in merged)
    return round(min(1.0, covered / (end_ms - start_ms)), 4)


def density_score(word_count: int, duration_ms: int, target_wps: float) -> float:
    """Words-per-second against a target rate (pure).

    Scores 1.0 at or above ``target_wps``; the actual/target ratio below it;
    0.0 when there are no words, the window is degenerate, or target is not
    positive.
    """
    if duration_ms <= 0 or word_count <= 0 or target_wps <= 0:
        return 0.0
    words_per_second = word_count / (duration_ms / 1000.0)
    return round(min(1.0, words_per_second / target_wps), 4)


def score_clip(components: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted sum of components normalized to 0..100 (pure, deterministic).

    Each component value must be finite and in 0..1; weights must be > 0.
    Components without a matching weight contribute 0; weights without a
    matching component still count toward the denominator. The result is
    clamped to 0..100 and rounded to 4 decimals.
    """
    if not weights:
        raise ValueError("weights must not be empty")
    for name, weight in weights.items():
        if not (weight > 0) or not math.isfinite(weight):
            raise ValueError(f"weight {name!r} must be a positive finite number")
    for name, value in components.items():
        if not math.isfinite(value) or value < 0.0 or value > 1.0:
            raise ValueError(f"component {name!r} must be finite in 0..1")
    weighted = sum(weights.get(key, 0.0) * value for key, value in components.items())
    weight_total = sum(weights.values())
    if weight_total <= 0:
        raise ValueError("sum of weights must be positive")
    return round(min(100.0, max(0.0, 100.0 * weighted / weight_total)), 4)


def compute_components(
    *,
    start_ms: int,
    end_ms: int,
    hook_spans: list[tuple[int, int]],
    claim_spans: list[tuple[int, int]],
    word_count: int,
    min_len_ms: int,
    max_len_ms: int,
    target_wps: float,
) -> dict[str, float]:
    """Derive the four scoring components for one candidate window (pure)."""
    duration = end_ms - start_ms
    return {
        "length_fit": length_fit(duration, min_len_ms, max_len_ms),
        "hook_coverage": span_coverage(start_ms, end_ms, hook_spans),
        "claim_coverage": span_coverage(start_ms, end_ms, claim_spans),
        "transcript_density": density_score(word_count, duration, target_wps),
    }


# ---------------------------------------------------------------------------
# Typed contracts
# ---------------------------------------------------------------------------


class ClipScoreRequest(BaseModel):
    """Inputs for one multi-factor scoring run over one candidate."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    weights: dict[str, float]
    min_len_ms: int = Field(default=1000, ge=0)
    max_len_ms: int = Field(default=60000, ge=0)
    target_wps: float = Field(default=2.5, gt=0)
    scorer_alias: str | None = Field(default=None, min_length=1, max_length=128)


class ClipScore(BaseModel):
    """One deterministic scoring row for one clip candidate."""

    model_config = ConfigDict(frozen=True)

    score_id: str = Field(default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128)
    candidate_id: str = Field(min_length=1, max_length=36)
    org_id: str = Field(min_length=1, max_length=64)
    components: dict[str, float] = Field(default_factory=dict)
    total: float = Field(ge=0.0, le=100.0)
    scorer_alias: str | None = Field(default=None, max_length=128)
    scored_at: datetime | None = None


# ---------------------------------------------------------------------------
# SQL row
# ---------------------------------------------------------------------------


class ClipScoreRecord(Base):
    """SQL row for one clip score (unique per org+candidate)."""

    __tablename__ = "clip_scores"
    __table_args__ = (
        sa.Index("uq_clip_scores_score_id", "score_id", unique=True),
        sa.Index("uq_clip_scores_org_candidate", "org_id", "candidate_id", unique=True),
        sa.Index("ix_clip_scores_org", "org_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    score_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    candidate_id: Mapped[str] = mapped_column(sa.String(36), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    components: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default=sa.text("'{}'")
    )
    total: Mapped[float] = mapped_column(sa.Float, nullable=False)
    scorer_alias: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    scored_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _score_from_row(row: ClipScoreRecord) -> ClipScore:
    return ClipScore(
        score_id=row.score_id,
        candidate_id=row.candidate_id,
        org_id=row.org_id,
        components=row.components or {},
        total=row.total,
        scorer_alias=row.scorer_alias,
        scored_at=_aware(row.scored_at),
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ClipScoreStore:
    """Typed persistence for clip scores plus scoring-context reads."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ClipScoreStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def load_scoring_context(
        self, candidate_id: str, org_id: str
    ) -> tuple[ClipCandidateRecord, list[tuple[int, int]], list[tuple[int, int]], int]:
        """Load the candidate window, overlapping HOOK/CLAIM spans, word count.

        Raises ValueError when the candidate is missing or already decided.
        All reads are org-scoped server-side.
        """
        # Deferred import: transcription_store <-> transcription form a cycle
        # that only resolves when transcription is imported first; keeping the
        # import here preserves safe module load order regardless of caller.
        from clipos_api.transcription_store import SegmentRecord, TranscriptRecord

        with self._sessions() as session:
            candidate = session.scalars(
                sa.select(ClipCandidateRecord).where(
                    ClipCandidateRecord.candidate_id == candidate_id,
                    ClipCandidateRecord.org_id == org_id,
                )
            ).first()
            if candidate is None:
                raise ValueError(f"clip candidate not found: {candidate_id}")
            if candidate.status in (
                ClipCandidateStatus.APPROVED.value,
                ClipCandidateStatus.REJECTED.value,
            ):
                raise ValueError(f"candidate already decided (status={candidate.status})")
            segments = session.scalars(
                sa.select(SemanticSegmentRecord).where(
                    SemanticSegmentRecord.org_id == org_id,
                    SemanticSegmentRecord.media_asset_id == candidate.media_asset_id,
                    SemanticSegmentRecord.end_ms > candidate.start_ms,
                    SemanticSegmentRecord.start_ms < candidate.end_ms,
                )
            ).all()
            hooks = [(s.start_ms, s.end_ms) for s in segments if s.kind == "HOOK"]
            claims = [(s.start_ms, s.end_ms) for s in segments if s.kind == "CLAIM"]
            transcript_ids = list(
                session.scalars(
                    sa.select(TranscriptRecord.transcript_id).where(
                        TranscriptRecord.org_id == org_id,
                        TranscriptRecord.media_asset_id == candidate.media_asset_id,
                    )
                ).all()
            )
            word_count = 0
            if transcript_ids:
                rows = session.scalars(
                    sa.select(SegmentRecord).where(
                        SegmentRecord.transcript_id.in_(transcript_ids),
                        SegmentRecord.org_id == org_id,
                        SegmentRecord.end_ms > candidate.start_ms,
                        SegmentRecord.start_ms < candidate.end_ms,
                    )
                ).all()
                word_count = sum(len(row.text.split()) for row in rows)
            return candidate, hooks, claims, word_count

    def record_score(self, score: ClipScore) -> ClipScore:
        """Persist one score idempotently; transition NEW -> SCORED; audit.

        An identical re-score (same components, total, and scorer_alias)
        returns the stored row without writing; different inputs raise.
        Decided candidates (APPROVED/REJECTED) reject scoring attempts.
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            candidate = session.scalars(
                sa.select(ClipCandidateRecord).where(
                    ClipCandidateRecord.candidate_id == score.candidate_id,
                    ClipCandidateRecord.org_id == score.org_id,
                )
            ).first()
            if candidate is None:
                raise ValueError(f"clip candidate not found: {score.candidate_id}")
            if candidate.status in (
                ClipCandidateStatus.APPROVED.value,
                ClipCandidateStatus.REJECTED.value,
            ):
                raise ValueError(f"candidate already decided (status={candidate.status})")
            existing = session.scalars(
                sa.select(ClipScoreRecord).where(
                    ClipScoreRecord.org_id == score.org_id,
                    ClipScoreRecord.candidate_id == score.candidate_id,
                )
            ).first()
            if existing is not None:
                stored = _score_from_row(existing)
                if (
                    stored.components == score.components
                    and stored.total == score.total
                    and stored.scorer_alias == score.scorer_alias
                ):
                    return stored
                raise ValueError(
                    f"clip candidate already scored with different fields: {score.candidate_id}"
                )
            row = ClipScoreRecord(
                score_id=score.score_id,
                candidate_id=score.candidate_id,
                org_id=score.org_id,
                components=dict(score.components),
                total=score.total,
                scorer_alias=score.scorer_alias,
                scored_at=now,
            )
            session.add(row)
            session.flush()
            transitioned = False
            if candidate.status == ClipCandidateStatus.NEW.value:
                candidate.status = ClipCandidateStatus.SCORED.value
                transitioned = True
            audit.record(
                "clip_scores.recorded",
                "clip_scores",
                f"score:{row.score_id}",
                {
                    "org_id": score.org_id,
                    "candidate_id": score.candidate_id,
                    "total": score.total,
                    "component_keys": sorted(score.components),
                    "scorer_alias": score.scorer_alias,
                },
            )
            if transitioned:
                audit.record(
                    "clip_candidate.scored",
                    "clip_candidates",
                    f"candidate:{score.candidate_id}",
                    {"org_id": score.org_id, "from_status": "NEW", "to_status": "SCORED"},
                )
            return _score_from_row(row)

    def get_score(self, candidate_id: str, org_id: str) -> ClipScore | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ClipScoreRecord).where(
                    ClipScoreRecord.candidate_id == candidate_id,
                    ClipScoreRecord.org_id == org_id,
                )
            ).first()
            return _score_from_row(row) if row is not None else None

    def list_by_org(self, org_id: str) -> list[ClipScore]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ClipScoreRecord)
                .where(ClipScoreRecord.org_id == org_id)
                .order_by(ClipScoreRecord.score_id)
            ).all()
            return [_score_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Scoring flow
# ---------------------------------------------------------------------------


def score_candidate(
    store: ClipScoreStore,
    candidate_id: str,
    request: ClipScoreRequest,
) -> ClipScore:
    """Compute four factors for one candidate and persist the score.

    Weights must be non-empty and use only known factor keys. The candidate
    must exist, be org-matching, and not be decided; violations raise
    ValueError with a message the route maps to 404/409/422.
    """
    if not request.weights:
        raise ValueError("weights must not be empty")
    unknown = set(request.weights) - set(COMPONENT_FACTORS)
    if unknown:
        raise ValueError(f"unknown weight factor(s): {', '.join(sorted(unknown))}")
    candidate, hook_spans, claim_spans, word_count = store.load_scoring_context(
        candidate_id, request.org_id
    )
    components = compute_components(
        start_ms=candidate.start_ms,
        end_ms=candidate.end_ms,
        hook_spans=hook_spans,
        claim_spans=claim_spans,
        word_count=word_count,
        min_len_ms=request.min_len_ms,
        max_len_ms=request.max_len_ms,
        target_wps=request.target_wps,
    )
    total = score_clip(components, request.weights)
    score = ClipScore(
        candidate_id=candidate_id,
        org_id=request.org_id,
        components=components,
        total=total,
        scorer_alias=request.scorer_alias,
    )
    return store.record_score(score)


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["clip-scores"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ClipScoreStore:
    return ClipScoreStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


@router.post(
    "/clip-candidates/{candidate_id}/scores",
    response_model=ClipScore,
    name="score_clip_candidate",
)
def score_clip_candidate_route(
    request: ClipScoreRequest,
    candidate_id: Annotated[str, Path(min_length=1, max_length=36)],
    store: Annotated[ClipScoreStore, Depends(_store)],
) -> ClipScore:
    """Compute and record the multi-factor score for one candidate."""
    try:
        return score_candidate(store, candidate_id, request)
    except ValueError as exc:
        message = str(exc)
        if "not found" in message:
            status = 404
        elif "already decided" in message or "already scored with different" in message:
            status = 409
        else:
            status = 422
        raise HTTPException(status_code=status, detail=message) from exc


@router.get(
    "/clip-candidates/{candidate_id}/scores",
    response_model=ClipScore | None,
    name="get_clip_candidate_score",
)
def get_clip_candidate_score_route(
    candidate_id: Annotated[str, Path(min_length=1, max_length=36)],
    store: Annotated[ClipScoreStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> ClipScore | None:
    """Return the stored score for one candidate, or null when unscored."""
    return store.get_score(candidate_id, org_id)


@router.get(
    "/clip-scores",
    response_model=list[ClipScore],
    name="list_clip_scores",
)
def list_clip_scores_route(
    store: Annotated[ClipScoreStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[ClipScore]:
    """List all scores for one org."""
    return store.list_by_org(org_id)
