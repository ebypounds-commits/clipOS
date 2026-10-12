"""Tests for caption tracks and SRT generation (CLIP-052)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.caption_tracks import (
    CaptionSegment,
    CaptionTrackRequest,
    CaptionTrackStore,
    segments_to_srt,
    validate_caption_segments,
)
from clipos_api.main import app
from clipos_api.render_project import RenderProject, RenderProjectStore


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-052-caption-tracks-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> CaptionTrackStore:
    return CaptionTrackStore.from_engine(engine)


def _valid_spec(duration_ms: int = 60000) -> dict:
    return {
        "version": 1,
        "inputs": {"candidate_id": "candidate-1"},
        "composition": {"aspect": "9:16", "duration_ms": duration_ms},
        "outputs": [{"platform": "youtube"}],
    }


def _seed_project(engine: Engine, duration_ms: int = 60000) -> None:
    from clipos_api.content_planning import ContentBrief, ContentPlanningStore
    from clipos_api.research import ResearchPacket, ResearchStore

    ResearchStore.from_engine(engine).register_packet(
        ResearchPacket(research_packet_id="packet-1", org_id="org-1", title="Fixture")
    )
    ContentPlanningStore.from_engine(engine).register_brief(
        ContentBrief(
            brief_id="brief-1",
            org_id="org-1",
            research_packet_id="packet-1",
            title="Fixture brief",
            objective="Objective",
        )
    )
    RenderProjectStore.from_engine(engine).register_project(
        RenderProject(
            render_project_id="render-1",
            org_id="org-1",
            brief_id="brief-1",
            name="Fixture render",
            spec=_valid_spec(duration_ms),
        )
    )


def _segments() -> list[CaptionSegment]:
    return [
        CaptionSegment(start_ms=0, end_ms=1500, text="First cue"),
        CaptionSegment(start_ms=1600, end_ms=3000, text="Second cue"),
    ]


def _request(**overrides: object) -> CaptionTrackRequest:
    payload: dict[str, object] = {
        "track_id": "track-1",
        "org_id": "org-1",
        "render_project_id": "render-1",
        "segments": _segments(),
    }
    payload.update(overrides)
    return CaptionTrackRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure validation and SRT
# ---------------------------------------------------------------------------


def test_validate_accepts_clean_segments() -> None:
    assert validate_caption_segments(_segments(), duration_ms=60000) == []


def test_validate_rejects_empty() -> None:
    errors = validate_caption_segments([], duration_ms=1000)
    assert any("non-empty" in error for error in errors)


def test_validate_rejects_overlap() -> None:
    segments = [
        CaptionSegment(start_ms=0, end_ms=2000, text="A"),
        CaptionSegment(start_ms=1500, end_ms=3000, text="B"),
    ]
    errors = validate_caption_segments(segments, duration_ms=10000)
    assert any("overlaps" in error for error in errors)


def test_validate_rejects_beyond_duration() -> None:
    segments = [CaptionSegment(start_ms=0, end_ms=9000, text="A")]
    errors = validate_caption_segments(segments, duration_ms=5000)
    assert any("exceeds render duration" in error for error in errors)


def test_segment_requires_ordered_window() -> None:
    with pytest.raises(ValueError, match="greater than"):
        CaptionSegment(start_ms=1000, end_ms=1000, text="X")


def test_srt_formatting_deterministic() -> None:
    srt = segments_to_srt(_segments())
    assert srt == (
        "1\n00:00:00,000 --> 00:00:01,500\nFirst cue\n\n"
        "2\n00:00:01,600 --> 00:00:03,000\nSecond cue\n"
    )
    # determinism
    assert segments_to_srt(_segments()) == srt


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_register_requires_existing_project(store: CaptionTrackStore) -> None:
    with pytest.raises(ValueError, match="render project not found"):
        store.register_track(_request())


def test_register_idempotent(store: CaptionTrackStore, engine: Engine) -> None:
    _seed_project(engine)
    first = store.register_track(_request())
    second = store.register_track(_request())
    assert first.track_id == second.track_id
    assert len(first.segments) == 2
    assert len(store.list_tracks("org-1", "render-1")) == 1


def test_register_conflict_on_different_fields(store: CaptionTrackStore, engine: Engine) -> None:
    _seed_project(engine)
    store.register_track(_request())
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_track(_request(language="fr"))


def test_register_rejects_segments_beyond_duration(
    store: CaptionTrackStore, engine: Engine
) -> None:
    _seed_project(engine, duration_ms=2000)
    with pytest.raises(ValueError, match="invalid caption track"):
        store.register_track(_request())


def test_get_track_org_scoped(store: CaptionTrackStore, engine: Engine) -> None:
    _seed_project(engine)
    store.register_track(_request())
    assert store.get_track("track-1", "org-1") is not None
    assert store.get_track("track-1", "org-other") is None
    assert store.get_track("missing", "org-1") is None


def test_list_tracks_missing_project_raises(store: CaptionTrackStore) -> None:
    with pytest.raises(ValueError, match="render project not found"):
        store.list_tracks("org-1", "missing")


def test_register_audit_recorded(store: CaptionTrackStore, engine: Engine, audit_log: Path) -> None:
    _seed_project(engine)
    store.register_track(_request())
    assert "caption_tracks.registered" in audit_log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/render-projects/{render_project_id}/caption-tracks" in paths
    assert "/caption-tracks/{track_id}" in paths


def test_router_meta() -> None:
    from clipos_api.caption_tracks import router

    assert router.tags == ["caption-tracks"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def _seed_project_http(client) -> None:
    assert (
        client.post(
            "/research-packets",
            json={"research_packet_id": "packet-1", "org_id": "org-1", "title": "Fixture"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/content-briefs",
            json={
                "brief_id": "brief-1",
                "org_id": "org-1",
                "research_packet_id": "packet-1",
                "title": "Fixture brief",
                "objective": "Objective",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/render-projects",
            json={
                "render_project_id": "render-1",
                "org_id": "org-1",
                "brief_id": "brief-1",
                "name": "Fixture render",
                "spec": _valid_spec(),
            },
        ).status_code
        == 200
    )


def test_route_register_and_fetch_roundtrip(client) -> None:
    _seed_project_http(client)
    created = client.post(
        "/render-projects/render-1/caption-tracks",
        json={
            "track_id": "track-1",
            "org_id": "org-1",
            "render_project_id": "render-1",
            "segments": [
                {"start_ms": 0, "end_ms": 1500, "text": "First cue"},
                {"start_ms": 1600, "end_ms": 3000, "text": "Second cue"},
            ],
        },
    )
    assert created.status_code == 200, created.text
    assert len(created.json()["segments"]) == 2

    fetched = client.get("/caption-tracks/track-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert fetched.json()["language"] == "en"

    listing = client.get("/render-projects/render-1/caption-tracks", params={"org_id": "org-1"})
    assert len(listing.json()) == 1


def test_route_path_body_mismatch_maps_to_422(client) -> None:
    _seed_project_http(client)
    response = client.post(
        "/render-projects/render-1/caption-tracks",
        json={
            "track_id": "track-1",
            "org_id": "org-1",
            "render_project_id": "other-render",
            "segments": [{"start_ms": 0, "end_ms": 100, "text": "X"}],
        },
    )
    assert response.status_code == 422


def test_route_missing_project_maps_to_404(client) -> None:
    response = client.post(
        "/render-projects/missing/caption-tracks",
        json={
            "track_id": "track-1",
            "org_id": "org-1",
            "render_project_id": "missing",
            "segments": [{"start_ms": 0, "end_ms": 100, "text": "X"}],
        },
    )
    assert response.status_code == 404
    fetched = client.get("/caption-tracks/missing", params={"org_id": "org-1"})
    assert fetched.status_code == 404
