"""Semantic segmentation and embedding pipeline (CLIP-041).

Provides:
- SemanticSegment and SegmentEmbedding Pydantic models (frozen, declarative)
- SemanticSegmentRecord / SegmentEmbeddingRecord SQLAlchemy rows on Base
- SemanticStore with from_engine, register_segment, record_embedding methods
- Pure helpers: chunk_transcript, fake_embed
- SemanticPipeline orchestrator
- Route definitions (imported and extended in main.py)

All model configs are frozen. No LLM, no network. Vectors stored via
vector_ref (refs-not-blobs). No pgvector.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Mapped, mapped_column

from clipos_api import audit
from clipos_api.repository import Base, get_engine

# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------


class SegmentKind(str):
    TOPIC = "TOPIC"
    SCENE = "SCENE"
    CLAIM = "CLAIM"
    HOOK = "HOOK"


# ----------------------------------------------------------------------
# Pydantic domain models (frozen)
# ----------------------------------------------------------------------


class SemanticSegment(BaseModel):
    """One semantic segment derived from a transcript/media asset."""

    model_config = ConfigDict(frozen=True)

    segment_pk_id: str = Field(min_length=1, max_length=36)
    media_asset_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=16)
    start_ms: int = Field(..., ge=0)
    end_ms: int = Field(..., ge=0)
    summary: str = Field(default="", max_length=512)
    source: str = Field(min_length=1, max_length=32)  # TRANSCRIPT | PROBE | MANUAL
    created_at: datetime | None = None

    @field_validator("end_ms")
    @classmethod
    def _end_not_before_start(cls, v: int, info: Any) -> int:
        start = info.data.get("start_ms")
        if start is not None and v < start:
            raise ValueError("end_ms must be >= start_ms")
        return v

    @field_validator("kind")
    @classmethod
    def _kind_must_be_valid(cls, v: str) -> str:
        if v not in (SegmentKind.TOPIC, SegmentKind.SCENE, SegmentKind.CLAIM, SegmentKind.HOOK):
            raise ValueError(f"kind must be one of {list(vars(SegmentKind).values())}, got: {v}")
        return v

    @field_validator("source")
    @classmethod
    def _source_must_be_valid(cls, v: str) -> str:
        if v not in ("TRANSCRIPT", "PROBE", "MANUAL"):
            raise ValueError(f"source must be one of TRANSCRIPT, PROBE, MANUAL, got: {v}")
        return v


class SegmentEmbedding(BaseModel):
    """One embedding vector reference for a semantic segment."""

    model_config = ConfigDict(frozen=True)

    embedding_id: str = Field(min_length=1, max_length=36)
    segment_pk_id: str = Field(min_length=1, max_length=36)
    org_id: str = Field(min_length=1, max_length=64)
    model_alias: str = Field(min_length=1, max_length=128)
    dim: int = Field(..., ge=1)
    vector_ref: str = Field(min_length=1, max_length=512)  # storage ref, NOT raw vector
    norm: float = Field(..., ge=0, le=1.0)
    created_at: datetime | None = None

    @field_validator("model_alias")
    @classmethod
    def _model_alias_must_be_registry_alias(cls, v: str) -> str:
        """Enforce that model_alias is a registry alias, never a vendor id."""
        import re

        if re.match(r"^[a-z0-9]+:[a-z0-9_.-]+$", v, re.IGNORECASE):
            raise ValueError(f"model_alias must be a registry alias, not a vendor ID: {v}")
        if re.match(r"^[a-z0-9]+/[a-z0-9_.-]+$", v, re.IGNORECASE):
            raise ValueError(f"model_alias must be a registry alias, not a vendor ID: {v}")
        return v


# ----------------------------------------------------------------------
# Declarative SQLAlchemy rows (never core Tables + scalar())
# ----------------------------------------------------------------------


class SemanticSegmentRecord(Base):
    """SQLAlchemy declarative model for semantic_segments."""

    __tablename__ = "semantic_segments"

    segment_pk_id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    media_asset_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    start_ms: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    end_ms: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    summary: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    source: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("org_id", "segment_pk_id", name="uq_semantic_segments_org_id_pk"),
        sa.Index("ix_semantic_segments_org_id", "org_id"),
        sa.Index("ix_semantic_segments_kind", "kind"),
    )

    @staticmethod
    def _contract(record) -> dict:
        return {
            "segment_pk_id": record.segment_pk_id,
            "media_asset_id": record.media_asset_id,
            "org_id": record.org_id,
            "kind": record.kind,
            "start_ms": record.start_ms,
            "end_ms": record.end_ms,
            "summary": record.summary,
            "source": record.source,
            "created_at": record.created_at,
        }


class SegmentEmbeddingRecord(Base):
    """SQLAlchemy declarative model for segment_embeddings."""

    __tablename__ = "segment_embeddings"

    embedding_id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    segment_pk_id: Mapped[str] = mapped_column(sa.String(36), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    model_alias: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    dim: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    vector_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    norm: Mapped[float] = mapped_column(sa.Float(), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("embedding_id", name="uq_segment_embeddings_embed_id"),
        sa.UniqueConstraint("segment_pk_id", "model_alias", name="uq_segment_embeddings_seg_model"),
        sa.Index("ix_segment_embeddings_org_id", "org_id"),
    )

    @staticmethod
    def _contract(record) -> dict:
        return {
            "embedding_id": record.embedding_id,
            "segment_pk_id": record.segment_pk_id,
            "org_id": record.org_id,
            "model_alias": record.model_alias,
            "dim": record.dim,
            "vector_ref": record.vector_ref,
            "norm": record.norm,
            "created_at": record.created_at,
        }


# ----------------------------------------------------------------------
# Store — Typed persistence
# ----------------------------------------------------------------------


class SemanticStore:
    """Typed persistence for semantic segments and embeddings.

    Notes
    -----
    - register_segment is idempotent per (org_id, segment_pk_id): conflicting
      re-registration raises ValueError.
    - record_embedding is append-only: (segment_pk_id, model_alias) unique
      constraint means conflicts raise IntegrityError.
    - get_segment / list_segments return None / [] when not found, scoped to org.
    """

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    @classmethod
    def from_engine(cls, engine: sa.Engine) -> SemanticStore:
        """Factory returning a SemanticStore bound to *engine*."""
        return cls(engine)

    def register_segment(self, segment: SemanticSegment) -> SemanticSegment:
        """Register one semantic segment; idempotent per (org_id, segment_pk_id)."""
        now = datetime.now(UTC)
        with _session_scope(self._engine) as session:
            existing = session.scalars(
                sa.select(SemanticSegmentRecord).where(
                    SemanticSegmentRecord.org_id == segment.org_id,
                    SemanticSegmentRecord.segment_pk_id == segment.segment_pk_id,
                )
            ).first()
            if existing is not None:
                stored = _segment_from_row(existing)
                if (
                    stored.kind != segment.kind
                    or stored.summary != segment.summary
                    or stored.source != segment.source
                ):
                    raise ValueError(
                        f"segment already registered with different fields: {segment.segment_pk_id}"
                    )
                audit.record(
                    "semantic.segment_created",
                    "semantic-store",
                    f"segment:{segment.segment_pk_id}",
                    {"org_id": segment.org_id},
                )
                return stored

            row = SemanticSegmentRecord(
                segment_pk_id=segment.segment_pk_id,
                media_asset_id=segment.media_asset_id,
                org_id=segment.org_id,
                kind=segment.kind,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                summary=segment.summary,
                source=segment.source,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "semantic.segment_created",
                "semantic-store",
                f"segment:{segment.segment_pk_id}",
                {"org_id": segment.org_id},
            )
            return _segment_from_row(row)

    def record_embedding(self, embedding: SegmentEmbedding) -> SegmentEmbedding:
        """Record one embedding vector reference; append-only."""
        now = datetime.now(UTC)
        with _session_scope(self._engine) as session:
            existing = session.scalars(
                sa.select(SegmentEmbeddingRecord).where(
                    SegmentEmbeddingRecord.segment_pk_id == embedding.segment_pk_id,
                    SegmentEmbeddingRecord.model_alias == embedding.model_alias,
                )
            ).first()
            if existing is not None:
                stored = _embedding_from_row(existing)
                if stored.vector_ref != embedding.vector_ref or stored.norm != embedding.norm:
                    raise ValueError(
                        f"embedding already recorded with different fields: "
                        f"{embedding.embedding_id}"
                    )
                audit.record(
                    "semantic.embedding_recorded",
                    "semantic-store",
                    f"embedding:{embedding.embedding_id}",
                    {"org_id": embedding.org_id, "model_alias": embedding.model_alias},
                )
                return stored

            row = SegmentEmbeddingRecord(
                embedding_id=embedding.embedding_id,
                segment_pk_id=embedding.segment_pk_id,
                org_id=embedding.org_id,
                model_alias=embedding.model_alias,
                dim=embedding.dim,
                vector_ref=embedding.vector_ref,
                norm=embedding.norm,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "semantic.embedding_recorded",
                "semantic-store",
                f"embedding:{embedding.embedding_id}",
                {"org_id": embedding.org_id, "model_alias": embedding.model_alias},
            )
            return _embedding_from_row(row)

    def get_segment(self, segment_pk_id: str, org_id: str) -> SemanticSegment | None:
        """Return the segment dict, scoped to org_id, or None."""
        with _session_scope(self._engine) as session:
            row = session.get(SemanticSegmentRecord, segment_pk_id)
            if row is None or row.org_id != org_id:
                return None
            return _segment_from_row(row)

    def list_segments(
        self, org_id: str, media_asset_id: str | None = None
    ) -> list[SemanticSegment]:
        """Return all segments for an org."""
        with _session_scope(self._engine) as session:
            stmt = sa.select(SemanticSegmentRecord).where(SemanticSegmentRecord.org_id == org_id)
            if media_asset_id is not None:
                stmt = stmt.where(SemanticSegmentRecord.media_asset_id == media_asset_id)
            rows = session.scalars(stmt.order_by(SemanticSegmentRecord.segment_pk_id)).all()
            return [_segment_from_row(row) for row in rows]

    def get_embedding(self, embedding_id: str, org_id: str) -> SegmentEmbedding | None:
        """Return the embedding dict, scoped to org_id, or None."""
        with _session_scope(self._engine) as session:
            row = session.get(SegmentEmbeddingRecord, embedding_id)
            if row is None or row.org_id != org_id:
                return None
            return _embedding_from_row(row)

    def list_embeddings(
        self, org_id: str, segment_pk_id: str | None = None
    ) -> list[SegmentEmbedding]:
        """Return embeddings for an org, optionally filtered by segment."""
        with _session_scope(self._engine) as session:
            stmt = sa.select(SegmentEmbeddingRecord).where(SegmentEmbeddingRecord.org_id == org_id)
            if segment_pk_id is not None:
                stmt = stmt.where(SegmentEmbeddingRecord.segment_pk_id == segment_pk_id)
            rows = session.scalars(stmt.order_by(SegmentEmbeddingRecord.embedding_id)).all()
            return [_embedding_from_row(row) for row in rows]


# ----------------------------------------------------------------------
# Pure helpers (NO LLM, NO network)
# ----------------------------------------------------------------------


def chunk_transcript(segments: list[Any], max_chars: int) -> list[list[Any]]:
    """Deterministic greedy chunking of transcript segments by character count.

    Splits the given flat list of TranscriptSegment-like objects into sub-lists
    such that the total character count of segment texts in each chunk does not
    exceed max_chars.  Each chunk preserves the original order.

    This is a pure function — no LLM, no network, deterministic.

    Returns a list of chunks, each chunk being a list of segment objects.
    """
    chunks: list[list[Any]] = []
    current: list[Any] = []
    current_chars = 0

    for seg in segments:
        n_chars = len(getattr(seg, "text", str(seg)))
        if current_chars + n_chars > max_chars and current:
            chunks.append(current)
            current = [seg]
            current_chars = n_chars
        else:
            current.append(seg)
            current_chars += n_chars

    if current:
        chunks.append(current)

    return chunks


def fake_embed(text: str, dim: int) -> list[float]:
    """Deterministic bag-of-words-ish hashing embedding.

    Hashes tokens from *text* into *dim* buckets using SHA-256, then
    L2-normalizes the resulting vector so that norm ~= 1.0.

    Pure function — no LLM, no network. Used by the pipeline in tests; real
    embedding model adapters come later behind a registry alias.
    """
    if dim <= 0:
        raise ValueError("dim must be > 0")

    tokens = text.lower().split()

    buckets: dict[int, int] = {}
    for token in tokens:
        h = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
        bucket = h % dim
        buckets[bucket] = buckets.get(bucket, 0) + 1

    norm_sq = sum(count**2 for count in buckets.values())
    if norm_sq == 0:
        return [0.0] * dim

    norm = norm_sq**0.5
    return [count / norm for count in [buckets.get(i, 0) for i in range(dim)]]


# ----------------------------------------------------------------------
# SemanticPipeline orchestrator
# ----------------------------------------------------------------------


class SemanticPipeline:
    """Orchestrator: given a media asset id + its transcript segments,

    produce semantic_segments rows (kind=TOPIC, one per chunk) and
    segment_embeddings rows (via fake_embed + vector_ref computed by caller).

    Audited: semantic.segment_created, semantic.embedding_recorded.
    """

    def __init__(self, store: SemanticStore) -> None:
        self._store = store

    def run(
        self,
        media_asset_id: str,
        org_id: str,
        transcript_segments: list[Any],
        max_chunk_chars: int = 512,
        source: str = "TRANSCRIPT",
    ) -> dict[str, Any]:
        """Execute the full semantic segmentation + embedding pipeline.

        Steps
        -----
        1. Chunk transcript_segments by max_chunk_chars -> list of chunks.
        2. For each chunk, register a semantic_segment (kind=TOPIC) via the store.
        3. For each registered segment, compute a fake embedding and record it.

        Returns a dict with 'segments' and 'embeddings' keys containing the
        created records.
        """
        chunks = chunk_transcript(transcript_segments, max_chunk_chars)

        created_segments: list[dict] = []
        created_embeddings: list[dict] = []

        for idx, chunk in enumerate(chunks):
            segment_pk_id = str(uuid.uuid4())[:36]

            segment = SemanticSegment(
                segment_pk_id=segment_pk_id,
                media_asset_id=media_asset_id,
                org_id=org_id,
                kind=SegmentKind.TOPIC,
                start_ms=chunk[0].start_ms if chunk else 0,
                end_ms=chunk[-1].end_ms if chunk else 0,
                summary="",
                source=source,
                created_at=datetime.now(UTC),
            )

            registered = self._store.register_segment(segment)
            created_segments.append(
                registered.model_dump() if hasattr(registered, "model_dump") else dict(registered)
            )

            summary_text = registered.summary or " ".join(getattr(s, "text", "") for s in chunk)
            vector = fake_embed(summary_text, dim=1536)

            embedding_id_val = str(uuid.uuid4())[:36]
            embedding = SegmentEmbedding(
                embedding_id=embedding_id_val,
                segment_pk_id=segment_pk_id,
                org_id=org_id,
                model_alias="text-embedding",
                dim=len(vector),
                vector_ref=f"ref://{segment_pk_id}:{embedding_id_val}",
                norm=1.0,
                created_at=datetime.now(UTC),
            )

            recorded = self._store.record_embedding(embedding)
            created_embeddings.append(
                recorded.model_dump() if hasattr(recorded, "model_dump") else dict(recorded)
            )

        return {
            "segments": created_segments,
            "embeddings": created_embeddings,
        }


# ----------------------------------------------------------------------
# Audit helper (read events from log)
# ----------------------------------------------------------------------


def _read_audit_events() -> list[dict]:
    from clipos_api import audit as audit_mod

    return audit_mod.read_events()


# ----------------------------------------------------------------------
# Session scope helper
# ----------------------------------------------------------------------


def _session_scope(engine: sa.Engine):
    from clipos_api.repository import session_scope

    return session_scope(engine)


# ----------------------------------------------------------------------
# Row -> model conversion helpers
# ----------------------------------------------------------------------


def _segment_from_row(row: SemanticSegmentRecord) -> SemanticSegment:
    return SemanticSegment(
        segment_pk_id=row.segment_pk_id,
        media_asset_id=row.media_asset_id,
        org_id=row.org_id,
        kind=row.kind,
        start_ms=row.start_ms,
        end_ms=row.end_ms,
        summary=row.summary or "",
        source=row.source,
        created_at=row.created_at,
    )


def _embedding_from_row(row: SegmentEmbeddingRecord) -> SegmentEmbedding:
    return SegmentEmbedding(
        embedding_id=row.embedding_id,
        segment_pk_id=row.segment_pk_id,
        org_id=row.org_id,
        model_alias=row.model_alias,
        dim=row.dim,
        vector_ref=row.vector_ref,
        norm=row.norm,
        created_at=row.created_at,
    )


# ----------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ----------------------------------------------------------------------


router = APIRouter(tags=["semantic"])

# ----------------------------------------------------------------------
# Routes — Depends providers BEFORE route decorators (CLIP-033 rule)
# ----------------------------------------------------------------------


def _semantic_store(engine: Annotated[sa.Engine, Depends(get_engine)]) -> SemanticStore:
    return SemanticStore.from_engine(engine)


@router.post(
    "/segments",
    response_model=SemanticSegment,
    status_code=201,
)
def create_segment(
    payload: SemanticSegment,
    store: Annotated[SemanticStore, Depends(_semantic_store)],
) -> SemanticSegment:
    """Register one semantic segment (idempotent, org-scoped)."""
    try:
        return store.register_segment(payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/segments/{segment_pk_id}", response_model=SemanticSegment)
def get_segment(
    store: Annotated[SemanticStore, Depends(_semantic_store)],
    segment_pk_id: Annotated[str, Path(min_length=1)],
    org_id: Annotated[str, Query(min_length=1)],
) -> SemanticSegment:
    """Fetch one semantic segment by id, scoped to the org."""
    found = store.get_segment(segment_pk_id, org_id)
    if found is None:
        raise HTTPException(status_code=404, detail="segment not found")
    return found


@router.get("/segments", response_model=list[SemanticSegment])
def list_segments(
    store: Annotated[SemanticStore, Depends(_semantic_store)],
    org_id: Annotated[str, Query(min_length=1)],
    media_asset_id: Annotated[str | None, Query()] = None,
) -> list[SemanticSegment]:
    """List semantic segments for an org, optionally filtered by media asset."""
    return store.list_segments(org_id, media_asset_id=media_asset_id)
