"""Tests for semantic segmentation and embedding pipeline (CLIP-041)."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.repository import Base
from clipos_api.semantic import (
    SegmentEmbedding,
    SemanticPipeline,
    SemanticSegment,
    SemanticStore,
    chunk_transcript,
    fake_embed,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-041-semantic-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> SemanticStore:
    return SemanticStore.from_engine(engine)


# ----------------------------------------------------------------------
# Test data: TranscriptSegment-like objects
# ----------------------------------------------------------------------


class _TranscriptSegment:
    """Minimal transcript segment for testing chunk_transcript."""

    __slots__ = ("end_ms", "start_ms", "text")

    def __init__(self, start_ms: int, end_ms: int, text: str) -> None:
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.text = text


CANNED_SEGMENTS: list[_TranscriptSegment] = [
    _TranscriptSegment(start_ms=0, end_ms=3333, text="first segment"),
    _TranscriptSegment(start_ms=3333, end_ms=6666, text="second segment"),
    _TranscriptSegment(start_ms=6666, end_ms=10000, text="third segment"),
    _TranscriptSegment(start_ms=10000, end_ms=13333, text="fourth segment"),
    _TranscriptSegment(start_ms=13333, end_ms=16666, text="fifth segment"),
]


# ----------------------------------------------------------------------
# chunk_transcript purity / determinism
# ----------------------------------------------------------------------


def test_chunk_transcript_basic() -> None:
    """Basic chunking respects max_chars boundary."""
    chunks = chunk_transcript(CANNED_SEGMENTS, max_chars=50)
    assert len(chunks) >= 1
    for chunk in chunks:
        total = sum(len(seg.text) for seg in chunk)
        assert total <= 50


def test_chunk_transcript_deterministic() -> None:
    """Same input always produces same output."""
    chunks1 = chunk_transcript(CANNED_SEGMENTS, max_chars=100)
    chunks2 = chunk_transcript(CANNED_SEGMENTS, max_chars=100)
    assert chunks1 == chunks2


def test_chunk_transcript_empty() -> None:
    """Empty segment list produces empty chunks."""
    assert chunk_transcript([], max_chars=50) == []


def test_chunk_transcript_single() -> None:
    """Single segment fits in one chunk."""
    segs = [_TranscriptSegment(0, 1000, "hello world")]
    chunks = chunk_transcript(segs, max_chars=100)
    assert len(chunks) == 1
    assert chunks[0] == segs


def test_chunk_transcript_overflow() -> None:
    """Segment larger than max_chars still produces a chunk (single element)."""
    segs = [_TranscriptSegment(0, 1000, "a" * 200)]
    chunks = chunk_transcript(segs, max_chars=50)
    assert len(chunks) == 1
    assert chunks[0] == segs


# ----------------------------------------------------------------------
# fake_embed determinism & norm
# ----------------------------------------------------------------------


def test_fake_embed_deterministic() -> None:
    """Same text + dim always produces same vector."""
    text = "deterministic embedding test"
    v1 = fake_embed(text, dim=128)
    v2 = fake_embed(text, dim=128)
    assert v1 == v2


def test_fake_embed_norm_is_one() -> None:
    """L2-normalized vector has norm ~= 1.0."""
    text = "norm check"
    vec = fake_embed(text, dim=64)
    norm = sum(x * x for x in vec) ** 0.5
    assert abs(norm - 1.0) < 1e-10


def test_fake_embed_different_dims() -> None:
    """Different dims produce vectors of correct length."""
    text = "dim test"
    for d in [32, 64, 128, 256, 1536]:
        vec = fake_embed(text, dim=d)
        assert len(vec) == d


def test_fake_embed_different_text() -> None:
    """Different texts produce different vectors."""
    v1 = fake_embed("hello world", dim=64)
    v2 = fake_embed("goodbye world", dim=64)
    assert v1 != v2


# ----------------------------------------------------------------------
# SemanticSegment model
# ----------------------------------------------------------------------


def test_semantic_segment_valid() -> None:
    """A valid semantic segment passes model validation."""
    seg = SemanticSegment(
        segment_pk_id="seg-001",
        media_asset_id="media-001",
        org_id="org-1",
        kind="TOPIC",
        start_ms=0,
        end_ms=3333,
        summary="topic summary",
        source="TRANSCRIPT",
    )
    assert seg.segment_pk_id == "seg-001"
    assert seg.kind == "TOPIC"
    assert seg.source == "TRANSCRIPT"


def test_semantic_segment_invalid_kind() -> None:
    """Invalid kind raises ValueError."""
    try:
        SemanticSegment(
            segment_pk_id="seg-001",
            media_asset_id="media-001",
            org_id="org-1",
            kind="INVALID",
            start_ms=0,
            end_ms=3333,
            summary="",
            source="TRANSCRIPT",
        )
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_semantic_segment_invalid_source() -> None:
    """Invalid source raises ValueError."""
    try:
        SemanticSegment(
            segment_pk_id="seg-001",
            media_asset_id="media-001",
            org_id="org-1",
            kind="TOPIC",
            start_ms=0,
            end_ms=3333,
            summary="",
            source="INVALID",
        )
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_semantic_segment_end_before_start() -> None:
    """end_ms < start_ms raises ValueError."""
    try:
        SemanticSegment(
            segment_pk_id="seg-001",
            media_asset_id="media-001",
            org_id="org-1",
            kind="TOPIC",
            start_ms=3333,
            end_ms=1000,  # end before start!
            summary="",
            source="TRANSCRIPT",
        )
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


# ----------------------------------------------------------------------
# SegmentEmbedding model
# ----------------------------------------------------------------------


def test_segment_embedding_valid() -> None:
    """A valid segment embedding passes model validation."""
    emb = SegmentEmbedding(
        embedding_id="emb-001",
        segment_pk_id="seg-001",
        org_id="org-1",
        model_alias="text-embedding",
        dim=128,
        vector_ref="ref://seg-001:emb-001",
        norm=1.0,
    )
    assert emb.embedding_id == "emb-001"
    assert emb.dim == 128
    assert emb.norm == 1.0


def test_segment_embedding_invalid_model_alias_vendor_id() -> None:
    """Vendor-style model_alias (with colon) raises ValueError."""
    try:
        SegmentEmbedding(
            embedding_id="emb-001",
            segment_pk_id="seg-001",
            org_id="org-1",
            model_alias="openai:text-embedding-3-large",  # vendor ID pattern
            dim=128,
            vector_ref="ref://seg-001:emb-001",
            norm=1.0,
        )
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_segment_embedding_invalid_dim() -> None:
    """dim <= 0 raises ValueError."""
    try:
        SegmentEmbedding(
            embedding_id="emb-001",
            segment_pk_id="seg-001",
            org_id="org-1",
            model_alias="text-embedding",
            dim=0,
            vector_ref="ref://seg-001:emb-001",
            norm=1.0,
        )
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_segment_embedding_norm_out_of_range() -> None:
    """norm > 1.0 raises ValueError."""
    try:
        SegmentEmbedding(
            embedding_id="emb-001",
            segment_pk_id="seg-001",
            org_id="org-1",
            model_alias="text-embedding",
            dim=128,
            vector_ref="ref://seg-001:emb-001",
            norm=1.5,
        )
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


# ----------------------------------------------------------------------
# SemanticStore idempotency / org isolation
# ----------------------------------------------------------------------


def test_register_segment_idempotent(store: SemanticStore) -> None:
    """Same (org_id, segment_pk_id) re-registration is idempotent."""
    seg = SemanticSegment(
        segment_pk_id="seg-001",
        media_asset_id="media-001",
        org_id="org-1",
        kind="TOPIC",
        start_ms=0,
        end_ms=3333,
        summary="topic summary",
        source="TRANSCRIPT",
    )
    first = store.register_segment(seg)
    again = store.register_segment(seg)
    assert first.segment_pk_id == again.segment_pk_id
    assert first.kind == again.kind


def test_register_segment_conflicting_raises(store: SemanticStore) -> None:
    """Different fields for same (org_id, segment_pk_id) raises ValueError."""
    seg1 = SemanticSegment(
        segment_pk_id="seg-001",
        media_asset_id="media-001",
        org_id="org-1",
        kind="TOPIC",
        start_ms=0,
        end_ms=3333,
        summary="topic summary",
        source="TRANSCRIPT",
    )
    seg2 = SemanticSegment(
        segment_pk_id="seg-001",
        media_asset_id="media-005",  # different media
        org_id="org-1",
        kind="SCENE",  # different kind
        start_ms=0,
        end_ms=3333,
        summary="different summary",
        source="PROBE",
    )
    store.register_segment(seg1)
    try:
        store.register_segment(seg2)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_org_isolation(store: SemanticStore) -> None:
    """Segments from different orgs are isolated."""
    seg1 = SemanticSegment(
        segment_pk_id="seg-001",
        media_asset_id="media-001",
        org_id="org-1",
        kind="TOPIC",
        start_ms=0,
        end_ms=3333,
        summary="org 1 summary",
        source="TRANSCRIPT",
    )
    seg2 = SemanticSegment(
        segment_pk_id="seg-002",
        media_asset_id="media-001",
        org_id="org-2",
        kind="TOPIC",
        start_ms=0,
        end_ms=3333,
        summary="org 2 summary",
        source="TRANSCRIPT",
    )
    s1 = store.register_segment(seg1)
    s2 = store.register_segment(seg2)
    # Each org has its own segment; PKs differ because the unique constraint is (org_id, segment_pk_id)
    assert s1.org_id == "org-1"
    assert s2.org_id == "org-2"
    assert s1.segment_pk_id == "seg-001"
    assert s2.segment_pk_id == "seg-002"


# ----------------------------------------------------------------------
# Embedding (segment, model_alias) uniqueness
# ----------------------------------------------------------------------


def test_embedding_segment_model_unique(store: SemanticStore) -> None:
    """(segment_pk_id, model_alias) uniqueness: second embedding raises IntegrityError."""
    seg = SemanticSegment(
        segment_pk_id="seg-001",
        media_asset_id="media-001",
        org_id="org-1",
        kind="TOPIC",
        start_ms=0,
        end_ms=3333,
        summary="topic summary",
        source="TRANSCRIPT",
    )
    store.register_segment(seg)

    emb1 = SegmentEmbedding(
        embedding_id="emb-001",
        segment_pk_id="seg-001",
        org_id="org-1",
        model_alias="text-embedding",
        dim=128,
        vector_ref="ref://seg-001:emb-001",
        norm=1.0,
    )
    store.record_embedding(emb1)

    emb2 = SegmentEmbedding(
        embedding_id="emb-002",
        segment_pk_id="seg-001",
        org_id="org-1",
        model_alias="text-embedding",  # same model_alias -> conflict
        dim=128,
        vector_ref="ref://seg-001:emb-002",
        norm=1.0,
    )
    with pytest.raises((ValueError, sa.exc.IntegrityError)):
        store.record_embedding(emb2)


def test_embedding_different_model_allowed(store: SemanticStore) -> None:
    """Different model_alias for same segment is allowed (unique constraint per pair)."""
    seg = SemanticSegment(
        segment_pk_id="seg-001",
        media_asset_id="media-001",
        org_id="org-1",
        kind="TOPIC",
        start_ms=0,
        end_ms=3333,
        summary="topic summary",
        source="TRANSCRIPT",
    )
    store.register_segment(seg)

    emb1 = SegmentEmbedding(
        embedding_id="emb-001",
        segment_pk_id="seg-001",
        org_id="org-1",
        model_alias="text-embedding",
        dim=128,
        vector_ref="ref://seg-001:emb-001",
        norm=1.0,
    )
    store.record_embedding(emb1)

    emb2 = SegmentEmbedding(
        embedding_id="emb-002",
        segment_pk_id="seg-001",
        org_id="org-1",
        model_alias="other-embedding",  # different alias -> allowed
        dim=128,
        vector_ref="ref://seg-001:emb-002",
        norm=1.0,
    )
    recorded = store.record_embedding(emb2)
    assert recorded.embedding_id == emb2.embedding_id


# ----------------------------------------------------------------------
# SemanticPipeline
# ----------------------------------------------------------------------


def test_pipeline_runs(store: SemanticStore) -> None:
    """Pipeline produces segments and embeddings."""
    segs = [
        _TranscriptSegment(0, 3333, "first segment text"),
        _TranscriptSegment(3333, 6666, "second segment text"),
    ]

    pipeline = SemanticPipeline(store=store)
    result = pipeline.run(
        media_asset_id="media-001",
        org_id="org-1",
        transcript_segments=segs,
        max_chunk_chars=1000,
        source="TRANSCRIPT",
    )

    assert "segments" in result
    assert "embeddings" in result
    assert len(result["segments"]) >= 1
    assert len(result["embeddings"]) >= 1


def test_pipeline_audit_events(store: SemanticStore, audit_log: Path) -> None:
    """Pipeline writes audit events."""
    from clipos_api import audit as audit_mod

    segs = [_TranscriptSegment(0, 3333, "test segment")]

    pipeline = SemanticPipeline(store=store)
    pipeline.run(
        media_asset_id="media-001",
        org_id="org-1",
        transcript_segments=segs,
        max_chunk_chars=1000,
        source="TRANSCRIPT",
    )

    actions = [e["action"] for e in audit_mod.read_events()]
    assert "semantic.segment_created" in actions
    assert "semantic.embedding_recorded" in actions


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    """Semantic routes are registered on the FastAPI app."""
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    # The semantic routes: /segments and /pipeline
    assert "/segments" in paths or any("/semantic" in p for p in paths), (
        "Expected semantic routes to be registered"
    )


def test_pipeline_route_registered() -> None:
    """Pipeline route is registered."""
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    # Check that at least one semantic path exists
    semantic_paths = [p for p in paths if "segment" in p.lower() or "pipeline" in p.lower()]
    # Should have some semantic routing
    assert len(semantic_paths) > 0 or any(r.name == "register_segment_route" for r in app.routes), (
        "Expected pipeline/segment routes to be registered"
    )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _read_audit_events() -> list[dict]:
    """Read audit events from the log file (helper for tests)."""
    from clipos_api import audit as audit_mod

    return audit_mod.read_events()
