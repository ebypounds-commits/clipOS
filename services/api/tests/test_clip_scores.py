"""Tests for multi-factor clip scoring (CLIP-043)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from clipos_api import audit
from clipos_api.clip_candidates import ClipCandidateSource, ClipCandidateStatus, ClipCandidateStore
from clipos_api.clip_scores import (
    COMPONENT_FACTORS,
    ClipScore,
    ClipScoreRequest,
    ClipScoreStore,
    compute_components,
    density_score,
    length_fit,
    score_candidate,
    score_clip,
    span_coverage,
)
from clipos_api.main import app
from clipos_api.semantic import SemanticSegmentRecord
from clipos_api.transcription_store import SegmentRecord, TranscriptRecord


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-043-clip-scores-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ClipScoreStore:
    return ClipScoreStore.from_engine(engine)


def _register_candidate(engine: Engine, candidate_id_org: str = "org-1") -> str:
    candidate_store = ClipCandidateStore.from_engine(engine)
    record = candidate_store.register_candidate(
        media_asset_id="asset-1",
        org_id=candidate_id_org,
        start_ms=1000,
        end_ms=10000,
        source=ClipCandidateSource.SEMANTIC.value,
        reason="fixture",
    )
    return record.candidate_id


def _seed_semantic(engine: Engine, org_id: str = "org-1") -> None:
    now = datetime.now(UTC)
    rows = [
        SemanticSegmentRecord(
            segment_pk_id="seg-hook",
            media_asset_id="asset-1",
            org_id=org_id,
            kind="HOOK",
            start_ms=1000,
            end_ms=3000,
            summary=None,
            source="MANUAL",
            created_at=now,
        ),
        SemanticSegmentRecord(
            segment_pk_id="seg-claim",
            media_asset_id="asset-1",
            org_id=org_id,
            kind="CLAIM",
            start_ms=4000,
            end_ms=6000,
            summary=None,
            source="MANUAL",
            created_at=now,
        ),
    ]
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()


def _seed_transcript(engine: Engine, org_id: str = "org-1") -> None:
    now = datetime.now(UTC)
    transcript = TranscriptRecord(
        transcript_id="tr-1",
        media_asset_id="asset-1",
        org_id=org_id,
        language="en",
        provider_alias="mock-alias",
        status="READY",
        model_hint=None,
        created_at=now,
        updated_at=now,
    )
    segment = SegmentRecord(
        segment_id="ts-1",
        transcript_id="tr-1",
        org_id=org_id,
        seq=1,
        start_ms=1000,
        end_ms=10000,
        text="one two three four five six seven eight nine ten",
        speaker=None,
    )
    with Session(engine) as session:
        session.add_all([transcript, segment])
        session.commit()


# ---------------------------------------------------------------------------
# Pure scorers
# ---------------------------------------------------------------------------


def test_length_fit_inside_window() -> None:
    assert length_fit(5000, 1000, 60000) == 1.0


def test_length_fit_below_window_scales() -> None:
    assert length_fit(500, 1000, 60000) == 0.5


def test_length_fit_above_window_scales() -> None:
    assert length_fit(120000, 1000, 60000) == 0.5


def test_length_fit_invalid_inputs_score_zero() -> None:
    assert length_fit(0, 1000, 60000) == 0.0
    assert length_fit(-5, 1000, 60000) == 0.0
    assert length_fit(5000, -1, 60000) == 0.0
    assert length_fit(5000, 70000, 60000) == 0.0


def test_span_coverage_empty_and_disjoint() -> None:
    assert span_coverage(0, 1000, []) == 0.0
    assert span_coverage(0, 1000, [(2000, 3000)]) == 0.0
    assert span_coverage(500, 500, [(0, 1000)]) == 0.0


def test_span_coverage_full_and_partial() -> None:
    assert span_coverage(0, 1000, [(0, 1000)]) == 1.0
    assert span_coverage(0, 1000, [(0, 500)]) == 0.5


def test_span_coverage_merges_overlaps_without_double_count() -> None:
    # two overlapping spans each cover 0..600 of a 1000ms window -> union 0.6
    assert span_coverage(0, 1000, [(0, 600), (400, 600)]) == 0.6


def test_span_coverage_clips_to_window() -> None:
    assert span_coverage(500, 1000, [(0, 750)]) == 0.5


def test_density_score_at_and_above_target() -> None:
    # 10 words in 4000ms = 2.5 wps exactly
    assert density_score(10, 4000, 2.5) == 1.0
    assert density_score(20, 4000, 2.5) == 1.0


def test_density_score_below_target_scales() -> None:
    # 5 words in 4000ms = 1.25 wps of 2.5 target -> 0.5
    assert density_score(5, 4000, 2.5) == 0.5


def test_density_score_invalid_inputs_score_zero() -> None:
    assert density_score(0, 4000, 2.5) == 0.0
    assert density_score(10, 0, 2.5) == 0.0
    assert density_score(10, 4000, 0.0) == 0.0


def test_score_clip_weighted_math() -> None:
    total = score_clip(
        {"length_fit": 1.0, "hook_coverage": 0.5},
        {"length_fit": 3.0, "hook_coverage": 1.0},
    )
    # (3*1.0 + 1*0.5) / 4 * 100 = 87.5
    assert total == 87.5


def test_score_clip_missing_component_counts_as_zero() -> None:
    total = score_clip({"length_fit": 1.0}, {"length_fit": 1.0, "hook_coverage": 1.0})
    assert total == 50.0


def test_score_clip_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        score_clip({"length_fit": 1.0}, {})
    with pytest.raises(ValueError, match="positive finite"):
        score_clip({"length_fit": 1.0}, {"length_fit": 0.0})
    with pytest.raises(ValueError, match="finite in 0..1"):
        score_clip({"length_fit": 1.5}, {"length_fit": 1.0})


def test_score_clip_clamps_to_bounds() -> None:
    assert score_clip({"length_fit": 1.0}, {"length_fit": 1.0}) == 100.0


def test_compute_components_covers_all_factors() -> None:
    components = compute_components(
        start_ms=1000,
        end_ms=10000,
        hook_spans=[(1000, 3000)],
        claim_spans=[(4000, 6000)],
        word_count=23,
        min_len_ms=1000,
        max_len_ms=60000,
        target_wps=2.5,
    )
    assert set(components) == set(COMPONENT_FACTORS)
    assert all(0.0 <= value <= 1.0 for value in components.values())


# ---------------------------------------------------------------------------
# Store persistence
# ---------------------------------------------------------------------------


def _request(**overrides: object) -> ClipScoreRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "weights": {"length_fit": 1.0, "hook_coverage": 1.0},
    }
    payload.update(overrides)
    return ClipScoreRequest(**payload)  # type: ignore[arg-type]


def test_record_score_transitions_candidate_to_scored(
    store: ClipScoreStore, engine: Engine, audit_log: Path
) -> None:
    candidate_id = _register_candidate(engine)
    score = ClipScore(
        candidate_id=candidate_id, org_id="org-1", components={"length_fit": 1.0}, total=100.0
    )
    stored = store.record_score(score)
    assert stored.score_id == score.score_id
    candidate = ClipCandidateStore.from_engine(engine).get(candidate_id, "org-1")
    assert candidate is not None
    assert candidate.status == ClipCandidateStatus.SCORED.value
    log = audit_log.read_text(encoding="utf-8")
    assert "clip_scores.recorded" in log
    assert "clip_candidate.scored" in log


def test_record_score_identical_rescore_is_noop(store: ClipScoreStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    score = ClipScore(
        candidate_id=candidate_id, org_id="org-1", components={"length_fit": 1.0}, total=100.0
    )
    first = store.record_score(score)
    second = store.record_score(score)
    assert first.score_id == second.score_id
    assert store.list_by_org("org-1") == [first]


def test_record_score_conflict_on_different_fields(store: ClipScoreStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    store.record_score(
        ClipScore(
            candidate_id=candidate_id, org_id="org-1", components={"length_fit": 1.0}, total=100.0
        )
    )
    with pytest.raises(ValueError, match="already scored with different fields"):
        store.record_score(
            ClipScore(
                candidate_id=candidate_id,
                org_id="org-1",
                components={"length_fit": 0.0},
                total=0.0,
            )
        )


def test_record_score_missing_candidate_raises(store: ClipScoreStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.record_score(ClipScore(candidate_id="missing", org_id="org-1", total=50.0))


def test_record_score_decided_candidate_rejected(store: ClipScoreStore, engine: Engine) -> None:
    from clipos_api.clip_candidates import ClipCandidateRecord

    candidate_id = _register_candidate(engine)
    with Session(engine) as session:
        record = session.get(ClipCandidateRecord, candidate_id)
        assert record is not None
        record.status = ClipCandidateStatus.APPROVED.value
        session.commit()
    with pytest.raises(ValueError, match="already decided"):
        store.record_score(
            ClipScore(candidate_id=candidate_id, org_id="org-1", components={}, total=0.0)
        )


def test_get_score_org_scoped(store: ClipScoreStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    store.record_score(
        ClipScore(
            candidate_id=candidate_id, org_id="org-1", components={"length_fit": 1.0}, total=100.0
        )
    )
    assert store.get_score(candidate_id, "org-1") is not None
    assert store.get_score(candidate_id, "org-other") is None


# ---------------------------------------------------------------------------
# Scoring flow
# ---------------------------------------------------------------------------


def test_score_candidate_full_flow(store: ClipScoreStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    _seed_semantic(engine)
    _seed_transcript(engine)
    score = score_candidate(store, candidate_id, _request())
    assert set(score.components) == set(COMPONENT_FACTORS)
    assert score.components["hook_coverage"] > 0.0
    assert score.components["claim_coverage"] > 0.0
    assert score.components["transcript_density"] > 0.0
    assert 0.0 <= score.total <= 100.0
    assert score.scorer_alias is None


def test_score_candidate_with_scorer_alias(store: ClipScoreStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    score = score_candidate(store, candidate_id, _request(scorer_alias="clip-heuristic-v1"))
    assert score.scorer_alias == "clip-heuristic-v1"


def test_score_candidate_rejects_unknown_weight_factor(
    store: ClipScoreStore, engine: Engine
) -> None:
    candidate_id = _register_candidate(engine)
    with pytest.raises(ValueError, match="unknown weight factor"):
        score_candidate(store, candidate_id, _request(weights={"bogus": 1.0}))


def test_score_candidate_rejects_empty_weights(store: ClipScoreStore, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    with pytest.raises(ValueError, match="must not be empty"):
        score_candidate(store, candidate_id, _request(weights={}))


def test_score_candidate_missing_candidate_raises(store: ClipScoreStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        score_candidate(store, "missing", _request())


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/clip-candidates/{candidate_id}/scores" in paths
    assert "/clip-scores" in paths


def test_router_meta() -> None:
    from clipos_api.clip_scores import router

    assert router.tags == ["clip-scores"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def test_route_score_and_fetch_roundtrip(client, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    _seed_semantic(engine)
    _seed_transcript(engine)
    response = client.post(
        f"/clip-candidates/{candidate_id}/scores",
        json={"org_id": "org-1", "weights": {"length_fit": 1.0, "hook_coverage": 1.0}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["candidate_id"] == candidate_id
    assert set(body["components"]) == set(COMPONENT_FACTORS)

    fetched = client.get(f"/clip-candidates/{candidate_id}/scores", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert fetched.json()["score_id"] == body["score_id"]

    listing = client.get("/clip-scores", params={"org_id": "org-1"})
    assert listing.status_code == 200
    assert len(listing.json()) == 1


def test_route_score_missing_candidate_maps_to_404(client) -> None:
    response = client.post(
        "/clip-candidates/missing/scores",
        json={"org_id": "org-1", "weights": {"length_fit": 1.0}},
    )
    assert response.status_code == 404


def test_route_score_conflict_maps_to_409(client, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    payload = {"org_id": "org-1", "weights": {"length_fit": 1.0}}
    assert client.post(f"/clip-candidates/{candidate_id}/scores", json=payload).status_code == 200
    # identical re-score is an idempotent 200
    assert client.post(f"/clip-candidates/{candidate_id}/scores", json=payload).status_code == 200
    # different weights change components/total -> 409
    changed = {"org_id": "org-1", "weights": {"length_fit": 1.0, "hook_coverage": 5.0}}
    assert client.post(f"/clip-candidates/{candidate_id}/scores", json=changed).status_code == 409


def test_route_score_unknown_factor_maps_to_422(client, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    response = client.post(
        f"/clip-candidates/{candidate_id}/scores",
        json={"org_id": "org-1", "weights": {"bogus": 1.0}},
    )
    assert response.status_code == 422


def test_route_get_unscored_returns_null(client, engine: Engine) -> None:
    candidate_id = _register_candidate(engine)
    response = client.get(f"/clip-candidates/{candidate_id}/scores", params={"org_id": "org-1"})
    assert response.status_code == 200
    assert response.json() is None


def test_route_score_decided_candidate_maps_to_409(client, engine: Engine) -> None:
    from clipos_api.clip_candidates import ClipCandidateRecord

    candidate_id = _register_candidate(engine)
    with Session(engine) as session:
        record = session.get(ClipCandidateRecord, candidate_id)
        assert record is not None
        record.status = ClipCandidateStatus.REJECTED.value
        session.commit()
    response = client.post(
        f"/clip-candidates/{candidate_id}/scores",
        json={"org_id": "org-1", "weights": {"length_fit": 1.0}},
    )
    assert response.status_code == 409
