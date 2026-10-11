"""Tests for transcription adapters and transcript storage (CLIP-040)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.transcription import (
    MockTranscriptionAdapter,
    Segment,
    TranscriptionResult,
    transcribe_and_store,
)
from clipos_api.transcription_store import TranscriptionStore


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base
    from clipos_api.transcription_store import TranscriptRecord  # noqa: F401

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-040-transcription-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> TranscriptionStore:
    return TranscriptionStore.from_engine(engine)


CANNED = "hello world segment"


def _adapter(n: int = 3) -> MockTranscriptionAdapter:
    return MockTranscriptionAdapter(canned_text=CANNED, num_segments=n)


def _segment(seq: int) -> Segment:
    return Segment(seq=seq, start_ms=seq * 1000, end_ms=seq * 1000 + 900, text=f"seg {seq}")


# ----------------------------------------------------------------------
# Mock adapter determinism
# ----------------------------------------------------------------------


def test_mock_adapter_deterministic() -> None:
    a1 = _adapter(3).transcribe("s3://x/a.mp4", "en")
    a2 = _adapter(3).transcribe("s3://x/a.mp4", "en")
    assert a1 == a2
    assert a1.provider_alias == "mock"
    assert len(a1.segments) == 3
    assert [s.seq for s in a1.segments] == [0, 1, 2]


def test_transcription_result_shape() -> None:
    result = _adapter(1).transcribe("ref", "de")
    assert isinstance(result, TranscriptionResult)
    assert result.language == "de"
    assert result.model_hint is None


# ----------------------------------------------------------------------
# register_transcript
# ----------------------------------------------------------------------


def test_register_transcript_idempotent(store: TranscriptionStore) -> None:
    first = store.register_transcript(
        media_asset_id="m1", org_id="org-1", language="en", provider_alias="mock"
    )
    again = store.register_transcript(
        media_asset_id="m1", org_id="org-1", language="en", provider_alias="mock"
    )
    assert first == again
    assert first["status"] == "PENDING"


def test_register_transcript_conflict_status(store: TranscriptionStore) -> None:
    first = store.register_transcript(
        media_asset_id="m1", org_id="org-1", language="en", provider_alias="alias-a"
    )
    second = store.register_transcript(
        media_asset_id="m1", org_id="org-1", language="en", provider_alias="alias-b"
    )
    # same natural key returns existing record regardless of alias differences
    assert first["transcript_id"] == second["transcript_id"]


def test_transcripts_org_isolated(store: TranscriptionStore) -> None:
    t1 = store.register_transcript("m1", "org-1", "en", "mock")
    t2 = store.register_transcript("m1", "org-2", "en", "mock")
    assert t1["transcript_id"] != t2["transcript_id"]


def test_language_stored_normalized(store: TranscriptionStore) -> None:
    t = store.register_transcript("m1", "org-1", "en", "mock")
    assert t["language"] == "en"


# ----------------------------------------------------------------------
# record_segments
# ----------------------------------------------------------------------


def test_record_segments_appends_and_returns(store: TranscriptionStore) -> None:
    t = store.register_transcript("m1", "org-1", "en", "mock")
    segs = [_segment(0), _segment(1), _segment(2)]
    stored = store.record_segments(t["transcript_id"], "org-1", segs)
    assert len(stored) == 3
    assert stored[0]["text"] == "seg 0"


def test_record_segments_rejects_conflicting_reappend(
    store: TranscriptionStore,
) -> None:
    t = store.register_transcript("m1", "org-1", "en", "mock")
    store.record_segments(t["transcript_id"], "org-1", [_segment(0), _segment(1)])
    conflicting = Segment(seq=1, start_ms=9999, end_ms=10999, text="changed")
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.record_segments(t["transcript_id"], "org-1", [conflicting])


def test_record_segments_rejects_seq_regression(store: TranscriptionStore) -> None:
    t = store.register_transcript("m1", "org-1", "en", "mock")
    store.record_segments(t["transcript_id"], "org-1", [_segment(0), _segment(2)])
    with pytest.raises(ValueError, match="must be > max existing seq"):
        store.record_segments(t["transcript_id"], "org-1", [_segment(1)])


def test_record_segments_requires_existing_transcript(
    store: TranscriptionStore,
) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.record_segments("missing", "org-1", [_segment(0)])


def test_record_segments_org_mismatch_raises(store: TranscriptionStore) -> None:
    t = store.register_transcript("m1", "org-1", "en", "mock")
    with pytest.raises(ValueError, match="org_id mismatch"):
        store.record_segments(t["transcript_id"], "org-9", [_segment(0)])


def test_list_segments_ordered(store: TranscriptionStore) -> None:
    t = store.register_transcript("m1", "org-1", "en", "mock")
    store.record_segments(t["transcript_id"], "org-1", [_segment(0), _segment(1), _segment(2)])
    listed = store.list_segments(t["transcript_id"], "org-1")
    assert [s["seq"] for s in listed] == [0, 1, 2]


def test_get_transcript_roundtrip(store: TranscriptionStore) -> None:
    registered = store.register_transcript("m1", "org-1", "en", "mock")
    stored = store.get_transcript(registered["transcript_id"], "org-1")
    assert stored == registered
    assert store.get_transcript(registered["transcript_id"], "org-9") is None


# ----------------------------------------------------------------------
# Flow
# ----------------------------------------------------------------------


def test_transcribe_and_store_flow(store: TranscriptionStore) -> None:
    transcript = transcribe_and_store(
        _adapter(3),
        store,
        media_asset_id="m1",
        org_id="org-1",
        storage_ref="s3://x/a.mp4",
        language="en",
    )
    assert transcript["provider_alias"] == "mock"
    segs = store.list_segments(transcript["transcript_id"], "org-1")
    assert [s["seq"] for s in segs] == [0, 1, 2]
    assert all(s["text"] == CANNED for s in segs)


def test_transcribe_and_store_idempotent(store: TranscriptionStore) -> None:
    t1 = transcribe_and_store(_adapter(2), store, "m1", "org-1", "s3://x/a.mp4", "en")
    # second run: same natural key -> same transcript; segments already exist
    t2 = transcribe_and_store(_adapter(2), store, "m1", "org-1", "s3://x/a.mp4", "en")
    assert t1["transcript_id"] == t2["transcript_id"]


def test_flow_writes_audit(store: TranscriptionStore) -> None:
    transcribe_and_store(_adapter(2), store, "m1", "org-1", "ref", "en")
    actions = [e["action"] for e in audit.read_events()]
    assert "transcription.completed" in actions


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/transcripts" in paths
    assert "/transcripts/{transcript_id}/segments" in paths
