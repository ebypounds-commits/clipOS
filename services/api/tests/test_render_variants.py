"""Tests for platform aspect variants, QC, and output manifests (CLIP-053)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.render_pipeline import (
    EXPECTED_RESOLUTIONS,
    RenderJobRequest,
    RenderJobStatus,
    RenderJobStore,
    RenderResult,
    SubprocessRenderRunner,
)
from clipos_api.render_project import RenderProject, RenderProjectStore
from clipos_api.render_variants import (
    RenderVariantStore,
    evaluate_qc,
    probe_output,
)


class MockRunner:
    """Deterministic test double recording argv invocations."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], timeout_seconds: int) -> RenderResult:
        self.calls.append(argv)
        # create the final argv path so size fallbacks see a file
        Path(argv[-1]).write_bytes(b"fake-mp4-bytes")
        return RenderResult(returncode=self.returncode, stderr_tail=self.stderr)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-053-render-variants-audit.log"
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
def store(engine: Engine, audit_log: Path) -> RenderVariantStore:
    return RenderVariantStore.from_engine(engine)


def _spec(duration_ms: int = 2000, outputs: list | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "inputs": {"candidate_id": "candidate-1"},
        "composition": {"aspect": "9:16", "duration_ms": duration_ms},
        "outputs": outputs if outputs is not None else [{"platform": "youtube"}],
    }


def _seed_job(engine: Engine, spec: dict[str, Any] | None = None) -> None:
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
            spec=spec if spec is not None else _spec(),
        )
    )
    RenderJobStore.from_engine(engine).create_job_for_project(
        RenderJobRequest(org_id="org-1", job_id="job-1"), "render-1"
    )


def _good_probe(aspect: str = "9:16", duration: float = 2.0) -> dict[str, Any]:
    width, height = EXPECTED_RESOLUTIONS[aspect]
    return {
        "format": {"duration": str(duration), "size": "12345"},
        "streams": [{"codec_type": "video", "width": width, "height": height}],
    }


def _make_input(workdir: Path) -> str:
    target = workdir / "in.mp4"
    target.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return str(target)


# ---------------------------------------------------------------------------
# Pure QC
# ---------------------------------------------------------------------------


def test_evaluate_qc_pass() -> None:
    passed, checks = evaluate_qc(_good_probe(), aspect="9:16", duration_ms=2000, size_bytes=100)
    assert passed is True
    assert checks["duration_ok"] is True
    assert checks["resolution_ok"] is True


def test_evaluate_qc_fails_zero_size() -> None:
    passed, _ = evaluate_qc(_good_probe(), aspect="9:16", duration_ms=2000, size_bytes=0)
    assert passed is False


def test_evaluate_qc_fails_duration_out_of_tolerance() -> None:
    passed, checks = evaluate_qc(
        _good_probe(duration=5.0), aspect="9:16", duration_ms=2000, size_bytes=10
    )
    assert passed is False
    assert checks["duration_ok"] is False


def test_evaluate_qc_fails_wrong_resolution() -> None:
    passed, checks = evaluate_qc(
        _good_probe(aspect="16:9"), aspect="9:16", duration_ms=2000, size_bytes=10
    )
    assert passed is False
    assert checks["resolution_ok"] is False


def test_evaluate_qc_fails_missing_duration() -> None:
    passed, checks = evaluate_qc(
        {"format": {}, "streams": []}, aspect="9:16", duration_ms=2000, size_bytes=10
    )
    assert passed is False
    assert checks["duration_ok"] is False


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_probe_output_real_file() -> None:
    with pytest.raises(ValueError, match="ffprobe failed"):
        probe_output("/nonexistent/file.mp4")


def test_build_argv_aspect_override() -> None:
    from clipos_api.render_pipeline import ASPECT_FILTERS, build_render_argv

    argv = build_render_argv(
        "/tmp/clipos-render/in.mp4",
        "/tmp/clipos-render/out.mp4",
        _spec(),
        aspect_override="16:9",
    )
    assert ASPECT_FILTERS["16:9"] in argv[argv.index("-vf") + 1]


def test_job_id_pattern_rejects_traversal() -> None:
    with pytest.raises(ValidationError):
        RenderJobRequest(org_id="org-1", job_id="../evil")


# ---------------------------------------------------------------------------
# Variant execution
# ---------------------------------------------------------------------------


def test_execute_variants_success(
    store: RenderVariantStore, engine: Engine, workdir: Path, audit_log: Path
) -> None:
    _seed_job(
        engine,
        spec=_spec(
            outputs=[
                {"platform": "youtube"},
                {"platform": "tiktok", "aspect": "1:1"},
            ]
        ),
    )
    job = store.execute_variants(
        MockRunner(),
        "job-1",
        "org-1",
        _make_input(workdir),
        prober=lambda path: _good_probe(aspect="9:16" if "youtube" in path else "1:1"),
    )
    assert job.status is RenderJobStatus.SUCCEEDED
    assert job.manifest is not None
    assert job.manifest["variant_count"] == 2
    assert [out["platform"] for out in job.manifest["outputs"]] == ["youtube", "tiktok"]

    outputs = store.list_outputs("org-1", "job-1")
    assert len(outputs) == 2
    aspects = {output.platform: output.aspect for output in outputs}
    assert aspects == {"youtube": "9:16", "tiktok": "1:1"}
    # per-output manifests persisted
    assert all(output.manifest["returncode"] == 0 for output in outputs)

    for output in outputs:
        report = store.get_qc_report(output.output_id, "org-1")
        assert report is not None
        assert report.status == "PASSED"

    log = audit_log.read_text(encoding="utf-8")
    assert "render_outputs.rendered" in log
    assert "qc_reports.recorded" in log
    assert "render_jobs.succeeded" in log


def test_execute_variants_qc_failure_fails_job(
    store: RenderVariantStore, engine: Engine, workdir: Path
) -> None:
    _seed_job(engine)
    job = store.execute_variants(
        MockRunner(),
        "job-1",
        "org-1",
        _make_input(workdir),
        prober=lambda path: _good_probe(duration=9.9),  # way off 2000ms
    )
    assert job.status is RenderJobStatus.FAILED
    assert job.error_ref is not None
    assert "QC failed" in job.error_ref
    # rows kept for diagnosis
    assert len(store.list_outputs("org-1", "job-1")) == 1
    report = store.get_qc_report("job-1:youtube", "org-1")
    assert report is not None
    assert report.status == "FAILED"


def test_execute_variants_ffmpeg_failure(
    store: RenderVariantStore, engine: Engine, workdir: Path
) -> None:
    _seed_job(engine)
    job = store.execute_variants(
        MockRunner(returncode=1, stderr="encode exploded"),
        "job-1",
        "org-1",
        _make_input(workdir),
        prober=lambda path: _good_probe(),
    )
    assert job.status is RenderJobStatus.FAILED
    assert job.error_ref is not None
    assert "encode exploded" in job.error_ref
    assert store.list_outputs("org-1", "job-1") == []


def test_execute_variants_invalid_aspect_leaves_pending(
    store: RenderVariantStore, engine: Engine, workdir: Path
) -> None:
    _seed_job(
        engine,
        spec=_spec(outputs=[{"platform": "youtube", "aspect": "2:1"}]),
    )
    with pytest.raises(ValueError, match="unsupported aspect"):
        store.execute_variants(
            MockRunner(), "job-1", "org-1", _make_input(workdir), prober=lambda p: _good_probe()
        )
    job = RenderJobStore.from_engine(engine).get_job("job-1", "org-1")
    assert job is not None
    assert job.status is RenderJobStatus.PENDING


def test_execute_variants_unsafe_platform_slug(
    store: RenderVariantStore, engine: Engine, workdir: Path
) -> None:
    _seed_job(engine, spec=_spec(outputs=[{"platform": "a/b"}]))
    with pytest.raises(ValueError, match="unsafe platform slug"):
        store.execute_variants(
            MockRunner(), "job-1", "org-1", _make_input(workdir), prober=lambda p: _good_probe()
        )


def test_execute_variants_status_gates(
    store: RenderVariantStore, engine: Engine, workdir: Path
) -> None:
    _seed_job(engine)
    store.execute_variants(
        MockRunner(), "job-1", "org-1", _make_input(workdir), prober=lambda p: _good_probe()
    )
    with pytest.raises(ValueError, match="already in status SUCCEEDED"):
        store.execute_variants(
            MockRunner(), "job-1", "org-1", _make_input(workdir), prober=lambda p: _good_probe()
        )
    with pytest.raises(ValueError, match="render job not found"):
        store.execute_variants(
            MockRunner(), "missing", "org-1", _make_input(workdir), prober=lambda p: _good_probe()
        )


def test_execute_variants_rerun_clears_stale_rows(
    store: RenderVariantStore, engine: Engine, workdir: Path
) -> None:
    _seed_job(engine)
    # first run fails QC -> FAILED rows exist
    store.execute_variants(
        MockRunner(),
        "job-1",
        "org-1",
        _make_input(workdir),
        prober=lambda p: _good_probe(duration=99),
    )
    assert store.list_outputs("org-1", "job-1")
    # retry with good probe -> rows replaced, job SUCCEEDED
    job = store.execute_variants(
        MockRunner(), "job-1", "org-1", _make_input(workdir), prober=lambda p: _good_probe()
    )
    assert job.status is RenderJobStatus.SUCCEEDED
    outputs = store.list_outputs("org-1", "job-1")
    assert len(outputs) == 1
    report = store.get_qc_report(outputs[0].output_id, "org-1")
    assert report is not None
    assert report.status == "PASSED"


def test_list_outputs_missing_job_raises(store: RenderVariantStore) -> None:
    with pytest.raises(ValueError, match="render job not found"):
        store.list_outputs("org-1", "missing")


def test_get_qc_report_absent_returns_none(store: RenderVariantStore) -> None:
    assert store.get_qc_report("missing", "org-1") is None


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/render-jobs/{job_id}/variants/execute" in paths
    assert "/render-jobs/{job_id}/outputs" in paths
    assert "/render-outputs/{output_id}/qc-report" in paths


def test_router_meta() -> None:
    from clipos_api.render_variants import router

    assert router.tags == ["render-variants"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def _seed_job_http(client) -> None:
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
                "spec": _spec(),
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/render-projects/render-1/jobs",
            json={"org_id": "org-1", "job_id": "job-1"},
        ).status_code
        == 200
    )


def test_route_missing_job_maps_to_404(client, workdir: Path) -> None:
    input_path = workdir / "missing-job-in.mp4"
    input_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    response = client.post(
        "/render-jobs/missing/variants/execute",
        params={"org_id": "org-1", "input_path": str(input_path)},
    )
    assert response.status_code == 404
    listing = client.get("/render-jobs/missing/outputs", params={"org_id": "org-1"})
    assert listing.status_code == 404


def test_route_output_listing_after_real_run(client, workdir: Path) -> None:
    """Route listing works after a store-driven run (real probe path intact)."""
    _seed_job_http(client)
    # drive through store with fake prober for determinism
    from clipos_api import main as main_mod

    engine = main_mod.database_engine()
    variant_store = RenderVariantStore.from_engine(engine)
    input_path = workdir / "route-in.mp4"
    input_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    variant_store.execute_variants(
        MockRunner(),
        "job-1",
        "org-1",
        str(input_path),
        prober=lambda p: _good_probe(),
    )
    listing = client.get("/render-jobs/job-1/outputs", params={"org_id": "org-1"})
    assert listing.status_code == 200
    outputs = listing.json()
    assert len(outputs) == 1
    report = client.get(
        f"/render-outputs/{outputs[0]['output_id']}/qc-report",
        params={"org_id": "org-1"},
    )
    assert report.status_code == 200
    assert report.json()["status"] == "PASSED"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_variants_and_qc(
    store: RenderVariantStore, engine: Engine, workdir: Path
) -> None:
    # duration 1000 matches the 1s lavfi clip so QC duration check passes
    _seed_job(
        engine,
        spec=_spec(
            duration_ms=1000,
            outputs=[{"platform": "youtube"}, {"platform": "tiktok", "aspect": "1:1"}],
        ),
    )
    input_path = workdir / "var-in.mp4"
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
    job = store.execute_variants(
        SubprocessRenderRunner(),
        "job-1",
        "org-1",
        str(input_path),
    )
    assert job.status is RenderJobStatus.SUCCEEDED, job.error_ref
    outputs = store.list_outputs("org-1", "job-1")
    assert len(outputs) == 2
    for output in outputs:
        report = store.get_qc_report(output.output_id, "org-1")
        assert report is not None
        assert report.status == "PASSED", report.checks
        assert (workdir / output.output_path).is_file()
