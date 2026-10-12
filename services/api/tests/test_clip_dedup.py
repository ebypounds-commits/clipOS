"""Tests for duplicate/content fingerprint checks (CLIP-044)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from clipos_api import audit
from clipos_api.clip_candidates import ClipCandidateSource, ClipCandidateStore
from clipos_api.clip_dedup import (
    CandidateFingerprint,
    ClipDedupStore,
    DuplicateCheckRequest,
    FingerprintKind,
    check_candidate_duplicates,
    normalize_text,
    overlap_over_shorter,
    transcript_fingerprint,
    window_fingerprint,
)
from clipos_api.main import app
from clipos_api.transcription_store import SegmentRecord, TranscriptRecord


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-044-clip-dedup-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ClipDedupStore:
    return ClipDedupStore.from_engine(engine)


def _register_candidate(
    engine: Engine,
    media_asset_id: str = "asset-1",
    org_id: str = "org-1",
    start_ms: int = 1000,
    end_ms: int = 10000,
) -> str:
    candidate_store = ClipCandidateStore.from_engine(engine)
    record = candidate_store.register_candidate(
        media_asset_id=media_asset_id,
        org_id=org_id,
        start_ms=start_ms,
        end_ms=end_ms,
        source=ClipCandidateSource.SEMANTIC.value,
        reason="fixture",
    )
    return record.candidate_id


def _seed_transcript(
    engine: Engine,
    text: str,
    org_id: str = "org-1",
    media_asset_id: str = "asset-1",
    transcript_id: str = "tr-1",
) -> None:
    now = datetime.now(UTC)
    transcript = TranscriptRecord(
        transcript_id=transcript_id,
        media_asset_id=media_asset_id,
        org_id=org_id,
        language="en",
        provider_alias="mock-alias",
        status="READY",
        model_hint=None,
        created_at=now,
        updated_at=now,
    )
    segment = SegmentRecord(
        segment_id=f"ts-{transcript_id}",
        transcript_id=transcript_id,
        org_id=org_id,
        seq=1,
        start_ms=0,
        end_ms=60000,
        text=text,
        speaker=None,
    )
    with Session(engine) as session:
        session.add_all([transcript, segment])
        session.commit()


def _request(**overrides: object) -> DuplicateCheckRequest:
    payload: dict[str, object] = {"org_id": "org-1"}
    payload.update(overrides)
    return DuplicateCheckRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_normalize_text_case_and_whitespace() -> None:
    assert normalize_text("  Hello   WORLD\tfresh ") == "hello world fresh"


def test_overlap_disjoint_and_degenerate() -> None:
    assert overlap_over_shorter(0, 1000, 2000, 3000) == 0.0
    assert overlap_over_shorter(0, 0, 0, 1000) == 0.0
    assert overlap_over_shorter(500, 500, 500, 500) == 0.0


def test_overlap_partial_and_contained() -> None:
    # 500ms shared of 1000ms shorter window -> 0.5
    assert overlap_over_shorter(0, 1000, 500, 5000) == 0.5
    # fully contained shorter window -> 1.0
    assert overlap_over_shorter(0, 5000, 1000, 2000) == 1.0
    # identical -> 1.0
    assert overlap_over_shorter(1000, 5000, 1000, 5000) == 1.0


def test_window_fingerprint_deterministic_and_distinct() -> None:
    base = window_fingerprint("asset-1", 1000, 5000)
    assert base == window_fingerprint("asset-1", 1000, 5000)
    assert len(base) == 64
    assert base != window_fingerprint("asset-2", 1000, 5000)
    assert base != window_fingerprint("asset-1", 1001, 5000)
    assert base != window_fingerprint("asset-1", 1000, 5001)


def test_transcript_fingerprint_normalizes_and_detects_empty() -> None:
    first = transcript_fingerprint("Same   TEXT here")
    second = transcript_fingerprint("same text HERE")
    assert first is not None
    assert first == second
    assert transcript_fingerprint("   ") is None
    assert transcript_fingerprint("") is None
    assert first != transcript_fingerprint("different text")


# ---------------------------------------------------------------------------
# Store persistence
# ---------------------------------------------------------------------------


def _fingerprint(candidate_id: str, value: str | None = None) -> CandidateFingerprint:
    return CandidateFingerprint(
        org_id="org-1",
        candidate_id=candidate_id,
        media_asset_id="asset-1",
        kind=FingerprintKind.WINDOW,
        value=value or window_fingerprint("asset-1", 1000, 10000),
    )


def test_persist_fingerprint_idempotent(store: ClipDedupStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    first = store.persist_fingerprint(_fingerprint(candidate_id))
    second = store.persist_fingerprint(_fingerprint(candidate_id))
    assert first.fingerprint_id == second.fingerprint_id
    assert len(store.list_fingerprints(candidate_id, "org-1")) == 1


def test_persist_fingerprint_conflict_on_different_value(
    store: ClipDedupStore, engine: Engine
) -> None:
    candidate_id = _register_candidate(engine)
    store.persist_fingerprint(_fingerprint(candidate_id))
    other_value = "a" * 64
    with pytest.raises(ValueError, match="already fingerprinted with different value"):
        store.persist_fingerprint(_fingerprint(candidate_id, value=other_value))


def test_list_fingerprints_org_scoped(store: ClipDedupStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    store.persist_fingerprint(_fingerprint(candidate_id))
    assert len(store.list_fingerprints(candidate_id, "org-1")) == 1
    assert store.list_fingerprints(candidate_id, "org-other") == []


def test_list_all_other_candidates_org_scoped_and_excludes_self(
    store: ClipDedupStore, engine: Engine
) -> None:
    first = _register_candidate(engine, start_ms=1000, end_ms=5000)
    second = _register_candidate(engine, start_ms=6000, end_ms=9000)
    others = store.list_all_other_candidates("org-1", first)
    assert [row.candidate_id for row in others] == [second]
    assert store.list_all_other_candidates("org-other", first) == []


# ---------------------------------------------------------------------------
# Duplicate-check flow
# ---------------------------------------------------------------------------


def test_check_persists_window_fingerprint_and_reports_none(
    store: ClipDedupStore, engine: Engine, audit_log: Path
) -> None:
    candidate_id = _register_candidate(engine)
    report = check_candidate_duplicates(store, candidate_id, _request())
    assert [fp.kind for fp in report.fingerprints] == [FingerprintKind.WINDOW]
    assert report.window_duplicates == []
    assert report.content_duplicates == []
    assert store.list_fingerprints(candidate_id, "org-1")
    log = audit_log.read_text(encoding="utf-8")
    assert "clip_candidates.duplicate_check" in log
    assert "candidate_fingerprints.persisted" in log


def test_check_detects_window_duplicate_same_asset(store: ClipDedupStore, engine: Engine) -> None:
    first = _register_candidate(engine, start_ms=1000, end_ms=10000)
    second = _register_candidate(engine, start_ms=5000, end_ms=15000)
    report = check_candidate_duplicates(store, first, _request(threshold=0.4))
    assert [finding.candidate_id for finding in report.window_duplicates] == [second]


def test_check_respects_threshold(store: ClipDedupStore, engine: Engine) -> None:
    first = _register_candidate(engine, start_ms=1000, end_ms=10000)
    _register_candidate(engine, start_ms=9500, end_ms=15000)  # 500/5500+ overlap small
    report = check_candidate_duplicates(store, first, _request(threshold=0.9))
    assert report.window_duplicates == []


def test_check_window_duplicate_requires_same_asset(store: ClipDedupStore, engine: Engine) -> None:
    first = _register_candidate(engine, media_asset_id="asset-1")
    _register_candidate(engine, media_asset_id="asset-2", start_ms=1000, end_ms=10000)
    report = check_candidate_duplicates(store, first, _request())
    assert report.window_duplicates == []


def test_check_detects_content_duplicate_across_assets(
    store: ClipDedupStore, engine: Engine
) -> None:
    shared = "identical spoken words for both clips"
    first = _register_candidate(engine, media_asset_id="asset-1")
    second = _register_candidate(engine, media_asset_id="asset-2")
    _seed_transcript(engine, shared, media_asset_id="asset-1", transcript_id="tr-a")
    _seed_transcript(engine, shared, media_asset_id="asset-2", transcript_id="tr-b")

    report = check_candidate_duplicates(store, first, _request())
    kinds = [fp.kind for fp in report.fingerprints]
    assert FingerprintKind.TRANSCRIPT in kinds
    assert [finding.candidate_id for finding in report.content_duplicates] == [second]

    # the other direction: second's check finds first
    reverse = check_candidate_duplicates(store, second, _request())
    assert [finding.candidate_id for finding in reverse.content_duplicates] == [first]


def test_check_no_transcript_skips_content_fingerprint(
    store: ClipDedupStore, engine: Engine
) -> None:
    candidate_id = _register_candidate(engine)
    report = check_candidate_duplicates(store, candidate_id, _request())
    assert [fp.kind for fp in report.fingerprints] == [FingerprintKind.WINDOW]


def test_check_rerun_is_idempotent_for_fingerprints(store: ClipDedupStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    first = check_candidate_duplicates(store, candidate_id, _request())
    second = check_candidate_duplicates(store, candidate_id, _request())
    assert [fp.fingerprint_id for fp in first.fingerprints] == [
        fp.fingerprint_id for fp in second.fingerprints
    ]
    assert len(store.list_fingerprints(candidate_id, "org-1")) == 1


def test_check_missing_candidate_raises(store: ClipDedupStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        check_candidate_duplicates(store, "missing", _request())


def test_check_rejects_bad_threshold(store: ClipDedupStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    with pytest.raises(ValueError, match="threshold"):
        check_candidate_duplicates(
            store, candidate_id, DuplicateCheckRequest(org_id="org-1", threshold=1.5)
        )


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/clip-candidates/{candidate_id}/duplicate-check" in paths
    assert "/clip-candidates/{candidate_id}/fingerprints" in paths


def test_router_meta() -> None:
    from clipos_api.clip_dedup import router

    assert router.tags == ["clip-dedup"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def test_route_duplicate_check_roundtrip(client, engine: Engine) -> None:
    first = _register_candidate(engine, start_ms=1000, end_ms=10000)
    second = _register_candidate(engine, start_ms=6000, end_ms=14000)
    response = client.post(
        f"/clip-candidates/{first}/duplicate-check",
        json={"org_id": "org-1", "threshold": 0.3},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["candidate_id"] == first
    assert body["threshold"] == 0.3
    assert [finding["candidate_id"] for finding in body["window_duplicates"]] == [second]
    assert body["content_duplicates"] == []

    fetched = client.get(f"/clip-candidates/{first}/fingerprints", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert len(fetched.json()) == 1
    assert fetched.json()[0]["kind"] == "WINDOW"


def test_route_missing_candidate_maps_to_404(client) -> None:
    response = client.post(
        "/clip-candidates/missing/duplicate-check",
        json={"org_id": "org-1"},
    )
    assert response.status_code == 404
    fetched = client.get("/clip-candidates/missing/fingerprints", params={"org_id": "org-1"})
    assert fetched.status_code == 404


def test_route_invalid_threshold_maps_to_422(client, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    response = client.post(
        f"/clip-candidates/{candidate_id}/duplicate-check",
        json={"org_id": "org-1", "threshold": 0},
    )
    assert response.status_code == 422


def test_route_default_threshold(client, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    response = client.post(
        f"/clip-candidates/{candidate_id}/duplicate-check",
        json={"org_id": "org-1"},
    )
    assert response.status_code == 200
    assert response.json()["threshold"] == 0.5
