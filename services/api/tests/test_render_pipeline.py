"""Tests for the FFmpeg worker/render pipeline (CLIP-051)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.render_pipeline import (
    ASPECT_FILTERS,
    RenderJob,
    RenderJobRequest,
    RenderJobStatus,
    RenderJobStore,
    RenderResult,
    SubprocessRenderRunner,
    build_render_argv,
    confine_path,
    spec_fingerprint,
)
from clipos_api.render_project import RenderProject, RenderProjectStore


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-051-render-pipeline-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def workdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "render-work"
    root.mkdir()
    monkeypatch.setenv("RENDER_WORKDIR", str(root))
    return root


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> RenderJobStore:
    return RenderJobStore.from_engine(engine)


class MockRunner:
    """Deterministic test double recording argv invocations."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], timeout_seconds: int) -> RenderResult:
        self.calls.append(argv)
        return RenderResult(returncode=self.returncode, stderr_tail=self.stderr)


def _valid_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "version": 1,
        "inputs": {"candidate_id": "candidate-1"},
        "composition": {
            "aspect": "9:16",
            "duration_ms": 2000,
            "captions": {"enabled": True, "style_ref": "brand:cap-1"},
            "audio_normalization": {"enabled": True, "target_lufs": -14},
        },
        "outputs": [{"platform": "youtube"}],
    }
    spec.update(overrides)
    return spec


def _seed_project(engine: Engine, spec: dict[str, Any] | None = None) -> None:
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
            spec=spec if spec is not None else _valid_spec(),
        )
    )


def _create_job(store: RenderJobStore, job_id: str = "job-1") -> RenderJob:
    return store.create_job_for_project(RenderJobRequest(org_id="org-1", job_id=job_id), "render-1")


def _make_input(workdir: Path, name: str = "in.mp4") -> str:
    target = workdir / name
    target.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return str(target)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_spec_fingerprint_deterministic_and_distinct() -> None:
    first = spec_fingerprint(_valid_spec())
    assert first == spec_fingerprint(_valid_spec())
    assert len(first) == 64
    assert first != spec_fingerprint(_valid_spec(version=1, outputs=[{"platform": "x"}]))
    # key order must not matter
    reordered = dict(reversed(list(_valid_spec().items())))
    assert spec_fingerprint(reordered) == first


def test_build_argv_trims_and_applies_aspect() -> None:
    argv = build_render_argv(
        "/tmp/clipos-render/in.mp4", "/tmp/clipos-render/out.mp4", _valid_spec()
    )
    assert argv[0] == "ffmpeg"
    assert "-t" in argv
    assert argv[argv.index("-t") + 1] == "2.000"
    assert argv[argv.index("-vf") + 1] == ASPECT_FILTERS["9:16"]
    assert argv[-1] == "/tmp/clipos-render/out.mp4"
    assert "+faststart" in argv


def test_build_argv_includes_loudnorm_when_enabled() -> None:
    argv = build_render_argv("in.mp4", "out.mp4", _valid_spec())
    assert "-af" in argv
    assert argv[argv.index("-af") + 1] == "loudnorm=I=-14.0"


def test_build_argv_omits_audio_filter_when_disabled() -> None:
    spec = _valid_spec()
    spec["composition"]["audio_normalization"] = {"enabled": False}
    argv = build_render_argv("in.mp4", "out.mp4", spec)
    assert "-af" not in argv


def test_build_argv_rejects_bad_aspect() -> None:
    spec = _valid_spec(composition={"aspect": "2:1", "duration_ms": 1000})
    with pytest.raises(ValueError, match="unsupported aspect"):
        build_render_argv("in.mp4", "out.mp4", spec)


def test_confine_path_rejects_escape(workdir: Path) -> None:
    inside = workdir / "ok.mp4"
    inside.write_bytes(b"x")
    assert confine_path(str(inside), must_exist=True) == inside.resolve()
    with pytest.raises(ValueError, match="outside render workdir"):
        confine_path(str(workdir.parent / "escape.mp4"))
    with pytest.raises(ValueError, match="outside render workdir"):
        confine_path("/etc/passwd")


def test_confine_path_missing_input(workdir: Path) -> None:
    with pytest.raises(ValueError, match="input file missing"):
        confine_path(str(workdir / "nope.mp4"), must_exist=True)


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------


def test_create_job_requires_existing_project(store: RenderJobStore) -> None:
    with pytest.raises(ValueError, match="render project not found"):
        store.create_job_for_project(RenderJobRequest(org_id="org-1", job_id="job-1"), "missing")


def test_create_job_idempotent(store: RenderJobStore, engine: Engine) -> None:
    _seed_project(engine)
    first = _create_job(store)
    second = _create_job(store)
    assert first.job_id == second.job_id
    assert first.status is RenderJobStatus.PENDING
    assert len(first.spec_fingerprint) == 64
    assert len(store.list_jobs("org-1", "render-1")) == 1


def test_create_job_conflict_on_different_project(store: RenderJobStore, engine: Engine) -> None:
    _seed_project(engine)
    RenderProjectStore.from_engine(engine).register_project(
        RenderProject(
            render_project_id="render-2",
            org_id="org-1",
            brief_id="brief-1",
            name="Second render",
            spec=_valid_spec(),
        )
    )
    _create_job(store, "job-1")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.create_job_for_project(RenderJobRequest(org_id="org-1", job_id="job-1"), "render-2")


def test_execute_success_records_manifest(
    store: RenderJobStore, engine: Engine, workdir: Path, audit_log: Path
) -> None:
    _seed_project(engine)
    _create_job(store)
    input_path = _make_input(workdir)
    output_path = str(workdir / "out.mp4")
    runner = MockRunner(returncode=0)
    job = store.execute_job(runner, "job-1", "org-1", input_path, output_path)
    assert job.status is RenderJobStatus.SUCCEEDED
    assert job.manifest is not None
    assert job.manifest["aspect"] == "9:16"
    assert job.manifest["captions_requested"] is True
    assert job.manifest["audio_normalized"] is True
    assert len(runner.calls) == 1
    log = audit_log.read_text(encoding="utf-8")
    assert "render_jobs.succeeded" in log
    assert "render_jobs.created" in log


def test_execute_failure_records_stderr(
    store: RenderJobStore, engine: Engine, workdir: Path, audit_log: Path
) -> None:
    _seed_project(engine)
    _create_job(store)
    runner = MockRunner(returncode=1, stderr="boom: bad input")
    job = store.execute_job(
        runner, "job-1", "org-1", _make_input(workdir), str(workdir / "out.mp4")
    )
    assert job.status is RenderJobStatus.FAILED
    assert job.error_ref is not None
    assert "boom" in job.error_ref
    assert "render_jobs.failed" in audit_log.read_text(encoding="utf-8")


def test_execute_refuses_succeeded_job(
    store: RenderJobStore, engine: Engine, workdir: Path
) -> None:
    _seed_project(engine)
    _create_job(store)
    store.execute_job(
        MockRunner(), "job-1", "org-1", _make_input(workdir), str(workdir / "out.mp4")
    )
    with pytest.raises(ValueError, match="already in status SUCCEEDED"):
        store.execute_job(
            MockRunner(),
            "job-1",
            "org-1",
            _make_input(workdir),
            str(workdir / "out2.mp4"),
        )


def test_execute_allows_retry_after_failure(
    store: RenderJobStore, engine: Engine, workdir: Path
) -> None:
    _seed_project(engine)
    _create_job(store)
    store.execute_job(
        MockRunner(returncode=1, stderr="first fails"),
        "job-1",
        "org-1",
        _make_input(workdir),
        str(workdir / "out.mp4"),
    )
    job = store.execute_job(
        MockRunner(returncode=0),
        "job-1",
        "org-1",
        _make_input(workdir),
        str(workdir / "out.mp4"),
    )
    assert job.status is RenderJobStatus.SUCCEEDED


def test_execute_rejects_path_escape(store: RenderJobStore, engine: Engine, workdir: Path) -> None:
    _seed_project(engine)
    _create_job(store)
    with pytest.raises(ValueError, match="outside render workdir"):
        store.execute_job(
            MockRunner(),
            "job-1",
            "org-1",
            "/etc/passwd",
            str(workdir / "out.mp4"),
        )


def test_execute_rejects_non_mp4_output(
    store: RenderJobStore, engine: Engine, workdir: Path
) -> None:
    _seed_project(engine)
    _create_job(store)
    with pytest.raises(ValueError, match="must end with .mp4"):
        store.execute_job(
            MockRunner(),
            "job-1",
            "org-1",
            _make_input(workdir),
            str(workdir / "out.avi"),
        )


def test_execute_missing_job_maps_to_not_found(store: RenderJobStore, workdir: Path) -> None:
    with pytest.raises(ValueError, match="render job not found"):
        store.execute_job(
            MockRunner(),
            "missing",
            "org-1",
            _make_input(workdir),
            str(workdir / "x.mp4"),
        )


def test_list_jobs_missing_project_raises(store: RenderJobStore) -> None:
    with pytest.raises(ValueError, match="render project not found"):
        store.list_jobs("org-1", "missing")


# ---------------------------------------------------------------------------
# Real ffmpeg integration (skipped when ffmpeg is absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_render_succeeds(store: RenderJobStore, engine: Engine, workdir: Path) -> None:
    _seed_project(engine)
    _create_job(store)
    input_path = workdir / "real-in.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=320x240:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(input_path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    output_path = workdir / "real-out.mp4"
    job = store.execute_job(
        SubprocessRenderRunner(),
        "job-1",
        "org-1",
        str(input_path),
        str(output_path),
    )
    assert job.status is RenderJobStatus.SUCCEEDED, job.error_ref
    assert output_path.is_file()
    assert output_path.stat().st_size > 0
    assert job.manifest is not None
    assert job.manifest["returncode"] == 0


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/render-projects/{render_project_id}/jobs" in paths
    assert "/render-jobs/{job_id}/execute" in paths
    assert "/render-jobs" in paths


def test_router_meta() -> None:
    from clipos_api.render_pipeline import router

    assert router.tags == ["render-jobs"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch):
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


def test_route_create_and_get_job(client) -> None:
    _seed_project_http(client)
    created = client.post(
        "/render-projects/render-1/jobs",
        json={"org_id": "org-1", "job_id": "job-1"},
    )
    assert created.status_code == 200
    assert created.json()["status"] == "PENDING"

    fetched = client.get("/render-jobs/job-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert fetched.json()["render_project_id"] == "render-1"

    listing = client.get(
        "/render-jobs", params={"org_id": "org-1", "render_project_id": "render-1"}
    )
    assert len(listing.json()) == 1


def test_route_create_job_missing_project_maps_to_404(client) -> None:
    response = client.post(
        "/render-projects/missing/jobs",
        json={"org_id": "org-1", "job_id": "job-1"},
    )
    assert response.status_code == 404


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_route_execute_real_render(client, workdir: Path) -> None:
    _seed_project_http(client)
    client.post(
        "/render-projects/render-1/jobs",
        json={"org_id": "org-1", "job_id": "job-1"},
    )
    input_path = workdir / "route-in.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=320x240:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(input_path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    response = client.post(
        "/render-jobs/job-1/execute",
        json={
            "org_id": "org-1",
            "input_path": str(input_path),
            "output_path": str(workdir / "route-out.mp4"),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert (workdir / "route-out.mp4").is_file()

    # re-execute refused
    again = client.post(
        "/render-jobs/job-1/execute",
        json={
            "org_id": "org-1",
            "input_path": str(input_path),
            "output_path": str(workdir / "route-out2.mp4"),
        },
    )
    assert again.status_code == 409


def test_route_execute_path_escape_maps_to_422(client, workdir: Path) -> None:
    _seed_project_http(client)
    client.post(
        "/render-projects/render-1/jobs",
        json={"org_id": "org-1", "job_id": "job-1"},
    )
    response = client.post(
        "/render-jobs/job-1/execute",
        json={
            "org_id": "org-1",
            "input_path": "/etc/passwd",
            "output_path": str(workdir / "out.mp4"),
        },
    )
    assert response.status_code == 422


def test_route_get_missing_job_maps_to_404(client) -> None:
    response = client.get("/render-jobs/missing", params={"org_id": "org-1"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# CLIP-052: caption burn-in and logo overlay
# ---------------------------------------------------------------------------


def test_escape_filter_path_escapes_colon_and_quote() -> None:
    from clipos_api.render_pipeline import escape_filter_path

    assert escape_filter_path("/tmp/a:b'c") == "/tmp/a\\:b\\'c"


def test_build_argv_merges_srt_into_video_chain() -> None:
    argv = build_render_argv(
        "/tmp/clipos-render/in.mp4",
        "/tmp/clipos-render/out.mp4",
        _valid_spec(),
        srt_path="/tmp/clipos-render/job-1.srt",
    )
    vf = argv[argv.index("-vf") + 1]
    assert ASPECT_FILTERS["9:16"] in vf
    assert "subtitles=filename='/tmp/clipos-render/job-1.srt'" in vf


def test_build_argv_logo_switches_to_filter_complex() -> None:
    argv = build_render_argv(
        "/tmp/clipos-render/in.mp4",
        "/tmp/clipos-render/out.mp4",
        _valid_spec(),
        srt_path="/tmp/clipos-render/job-1.srt",
        logo_path="/tmp/clipos-render/brand.png",
    )
    assert "-filter_complex" in argv
    assert "-vf" not in argv
    fc = argv[argv.index("-filter_complex") + 1]
    assert "subtitles=filename=" in fc
    assert "overlay=W-w-16:16" in fc
    assert "[v]" in argv
    assert "0:a?" in argv
    # both inputs present
    assert argv.count("-i") == 2


def _valid_spec_with_extras(**overrides) -> dict:
    spec = _valid_spec(**overrides)
    spec["inputs"] = {"candidate_id": "candidate-1"}
    return spec


def _seed_project_with_captions(engine: Engine, logo_ref: str | None = None) -> None:
    from clipos_api.caption_tracks import CaptionSegment, CaptionTrackRequest, CaptionTrackStore
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
    spec = _valid_spec()
    spec["composition"]["captions"] = {"enabled": True, "style_ref": "brand:cap-1"}
    inputs = {"candidate_id": "candidate-1"}
    if logo_ref is not None:
        inputs["logo_ref"] = logo_ref
    spec["inputs"] = inputs
    RenderProjectStore.from_engine(engine).register_project(
        RenderProject(
            render_project_id="render-1",
            org_id="org-1",
            brief_id="brief-1",
            name="Fixture render",
            spec=spec,
        )
    )
    CaptionTrackStore.from_engine(engine).register_track(
        CaptionTrackRequest(
            track_id="track-1",
            org_id="org-1",
            render_project_id="render-1",
            segments=[CaptionSegment(start_ms=0, end_ms=1500, text="Burned cue")],
        )
    )


def test_execute_with_captions_track_burns_srt(
    store: RenderJobStore, engine: Engine, workdir: Path
) -> None:
    from clipos_api.caption_tracks import CaptionSegment, CaptionTrackStore
    from clipos_api.caption_tracks import CaptionTrackRequest as _Req
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
    spec = _valid_spec()
    spec["composition"]["captions"] = {"enabled": True, "style_ref": None}
    RenderProjectStore.from_engine(engine).register_project(
        RenderProject(
            render_project_id="render-1",
            org_id="org-1",
            brief_id="brief-1",
            name="Fixture render",
            spec=spec,
        )
    )
    CaptionTrackStore.from_engine(engine).register_track(
        _Req(
            track_id="track-1",
            org_id="org-1",
            render_project_id="render-1",
            segments=[CaptionSegment(start_ms=0, end_ms=1500, text="Burned cue")],
        )
    )
    _create_job(store)
    runner = MockRunner()
    job = store.execute_job(
        runner,
        "job-1",
        "org-1",
        _make_input(workdir),
        str(workdir / "out.mp4"),
    )
    assert job.status is RenderJobStatus.SUCCEEDED
    assert job.manifest is not None
    assert job.manifest["captions_burned"] is True
    assert any("subtitles=filename=" in arg for arg in runner.calls[0])
    srt_file = workdir / "job-1.srt"
    assert srt_file.is_file()
    assert "Burned cue" in srt_file.read_text(encoding="utf-8")


def test_execute_captions_enabled_without_track_still_succeeds(
    store: RenderJobStore, engine: Engine, workdir: Path
) -> None:
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
    spec = _valid_spec()
    spec["composition"]["captions"] = {"enabled": True}
    RenderProjectStore.from_engine(engine).register_project(
        RenderProject(
            render_project_id="render-1",
            org_id="org-1",
            brief_id="brief-1",
            name="Fixture render",
            spec=spec,
        )
    )
    _create_job(store)
    runner = MockRunner()
    job = store.execute_job(
        runner, "job-1", "org-1", _make_input(workdir), str(workdir / "out.mp4")
    )
    assert job.status is RenderJobStatus.SUCCEEDED
    assert job.manifest is not None
    assert job.manifest["captions_requested"] is True
    assert job.manifest["captions_burned"] is False


def test_execute_with_logo_ref_overlays(
    store: RenderJobStore, engine: Engine, workdir: Path
) -> None:
    _seed_project_with_captions(engine, logo_ref="brand-logo.png")
    (workdir / "brand-logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    _create_job(store)
    runner = MockRunner()
    job = store.execute_job(
        runner, "job-1", "org-1", _make_input(workdir), str(workdir / "out.mp4")
    )
    assert job.status is RenderJobStatus.SUCCEEDED
    assert job.manifest is not None
    assert job.manifest["logo_overlaid"] is True
    assert job.manifest["captions_burned"] is True
    argv = runner.calls[0]
    assert "-filter_complex" in argv
    assert argv.count("-i") == 2


def test_execute_missing_logo_fails_before_running(
    store: RenderJobStore, engine: Engine, workdir: Path
) -> None:
    _seed_project_with_captions(engine, logo_ref="missing-logo.png")
    _create_job(store)
    with pytest.raises(ValueError, match="input file missing"):
        store.execute_job(
            MockRunner(), "job-1", "org-1", _make_input(workdir), str(workdir / "out.mp4")
        )
    # job untouched -> still retryable
    job = store.get_job("job-1", "org-1")
    assert job is not None
    assert job.status is RenderJobStatus.PENDING


def test_execute_logo_ref_escapes_workdir(
    store: RenderJobStore, engine: Engine, workdir: Path
) -> None:
    _seed_project_with_captions(engine, logo_ref="../escape.png")
    _create_job(store)
    with pytest.raises(ValueError, match="outside render workdir"):
        store.execute_job(
            MockRunner(), "job-1", "org-1", _make_input(workdir), str(workdir / "out.mp4")
        )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_captions_burn_succeeds(
    store: RenderJobStore, engine: Engine, workdir: Path
) -> None:
    from clipos_api.caption_tracks import CaptionSegment, CaptionTrackStore
    from clipos_api.caption_tracks import CaptionTrackRequest as _Req
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
    spec = _valid_spec()
    spec["composition"]["captions"] = {"enabled": True, "style_ref": None}
    RenderProjectStore.from_engine(engine).register_project(
        RenderProject(
            render_project_id="render-1",
            org_id="org-1",
            brief_id="brief-1",
            name="Fixture render",
            spec=spec,
        )
    )
    CaptionTrackStore.from_engine(engine).register_track(
        _Req(
            track_id="track-1",
            org_id="org-1",
            render_project_id="render-1",
            segments=[CaptionSegment(start_ms=0, end_ms=1500, text="Burned cue")],
        )
    )
    _create_job(store)
    input_path = workdir / "cap-in.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=320x240:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(input_path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    output_path = workdir / "cap-out.mp4"
    job = store.execute_job(
        SubprocessRenderRunner(),
        "job-1",
        "org-1",
        str(input_path),
        str(output_path),
    )
    assert job.status is RenderJobStatus.SUCCEEDED, job.error_ref
    assert output_path.is_file()
    assert job.manifest is not None
    assert job.manifest["captions_burned"] is True
