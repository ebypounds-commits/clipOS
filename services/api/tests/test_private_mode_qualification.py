"""Tests for the private-mode qualification harness (CLIP-098).

Drives the harness against a live in-process FastAPI app so the harness
itself is covered: the pipeline must complete, every private-mode invariant
must hold, the report must be schema-stable, and a deliberately regressed
policy evaluator must flip the verdict to FAIL rather than silently pass.

The harness is loaded by path and receives a TestClient-shaped transport,
mirroring `test_security_qualification.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit


def _find_repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "10_TICKETS.yaml").exists():
            return candidate
    return Path(__file__).resolve().parents[3]


REPO_ROOT = _find_repo_root()


def _load_harness():
    """Import the harness by path (dataclasses need sys.modules registration)."""
    spec = importlib.util.spec_from_file_location(
        "private_mode_qualification",
        REPO_ROOT / "scripts" / "private_mode_qualification.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["private_mode_qualification"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("private_mode_qualification", None)
        raise
    return module


@pytest.fixture()
def engine(monkeypatch: pytest.MonkeyPatch) -> Engine:
    path = Path("/tmp") / "clipos-098-qual-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    import clipos_api.main  # noqa: F401 - registers all metadata
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def live_client(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


class _TransportClient:
    """Adapts TestClient to the harness's urllib-shaped Client surface."""

    def __init__(self, test_client: TestClient, base_url: str = "http://qual.local") -> None:
        self._client = test_client
        self.base_url = base_url
        self.seen: list[str] = []

    def request(
        self, method: str, path: str, body: dict | None = None, params: dict | None = None
    ) -> tuple[int, object]:
        url = path
        if params:
            url = f"{path}?{'&'.join(f'{k}={v}' for k, v in params.items() if v is not None)}"
        response = self._client.request(method, url, json=body)
        raw = response.text
        self.seen.append(raw)
        try:
            return response.status_code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return response.status_code, raw


# ---------------------------------------------------------------------------
# Harness behaviour against the real app
# ---------------------------------------------------------------------------


def test_qualification_passes_against_live_app(live_client: TestClient) -> None:
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")

    assert report["summary"]["verdict"] == "PASS", json.dumps(report["findings"], indent=2)
    assert report["summary"]["steps_failed"] == 0
    assert report["summary"]["invariants_failed"] == 0


def test_pipeline_walks_every_stage(live_client: TestClient) -> None:
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")

    stages = set(report["stages"])
    for expected in (
        "research",
        "planning",
        "produce",
        "render",
        "publish",
        "analytics",
        "revenue",
        "experiments",
    ):
        assert expected in stages, f"stage not walked: {expected}"
    # Every stage must be recorded with at least one PASS or SKIP step, and
    # no stage may record a failure â€” a stage that cannot run in this
    # environment is reported as skipped, never silently dropped.
    for stage, counts in report["stages"].items():
        assert counts["failed"] == 0, f"stage recorded failures: {stage}"
        assert counts["passed"] + counts["skipped"] > 0, f"stage empty: {stage}"


def test_render_stage_is_reported_even_when_media_is_unavailable(
    live_client: TestClient,
) -> None:
    """A stage that cannot run must be reported as skipped, not omitted."""
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")

    render_steps = [s for s in report["steps"] if s["stage"] == "render"]
    assert render_steps, "render stage produced no steps at all"
    assert all(s["status"] in ("PASS", "SKIP") for s in render_steps)


def test_every_private_mode_invariant_is_covered(live_client: TestClient) -> None:
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")
    covered = {i["check_id"] for i in report["invariants"]}
    assert covered == {f"P{i}" for i in range(1, 7)}


def test_report_schema_is_stable(live_client: TestClient) -> None:
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")

    assert report["tool"] == "clipos-private-mode-qualification"
    assert report["version"] == 1
    for key in (
        "generated_at",
        "base_url",
        "org_id",
        "summary",
        "stages",
        "steps",
        "invariants",
        "findings",
    ):
        assert key in report, f"missing report key: {key}"

    summary = report["summary"]
    assert set(summary) == {
        "steps",
        "steps_failed",
        "invariants",
        "invariants_failed",
        "failed",
        "skipped",
        "verdict",
    }
    assert summary["steps"] == len(report["steps"])
    assert summary["invariants"] == len(report["invariants"])
    assert summary["failed"] == summary["steps_failed"] + summary["invariants_failed"]

    for step in report["steps"]:
        assert set(step) == {"stage", "name", "status", "http_status", "detail"}
        assert step["status"] in ("PASS", "FAIL", "SKIP")
    for invariant in report["invariants"]:
        assert set(invariant) == {"check_id", "title", "status", "evidence", "details"}
        assert invariant["status"] in ("PASS", "FAIL", "SKIP")


def test_report_is_json_serializable(live_client: TestClient) -> None:
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")
    json.dumps(report)


def test_findings_only_contain_failures(live_client: TestClient) -> None:
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")
    failing = {i["check_id"] for i in report["invariants"] if i["status"] == "FAIL"}
    assert {f["check_id"] for f in report["findings"]} == failing


# ---------------------------------------------------------------------------
# The harness must not pass a broken target
# ---------------------------------------------------------------------------


def test_observe_invariant_fails_when_mode_check_is_broken(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regression that lets OBSERVE publish must flip P1 to FAIL."""
    from clipos_api import publishing

    original = publishing.evaluate_publish_policy

    def leaky(policy, visibility, operating_mode, account_approved=False):
        # Simulate a regression: mode restrictions are ignored.
        return True, "allowed"

    monkeypatch.setattr(publishing, "evaluate_publish_policy", leaky)
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")

    failing = {i["check_id"] for i in report["invariants"] if i["status"] == "FAIL"}
    assert "P1" in failing
    assert report["summary"]["verdict"] == "FAIL"
    # Restore for other tests in the process.
    monkeypatch.setattr(publishing, "evaluate_publish_policy", original)


def test_public_default_invariant_fails_when_public_is_allowed(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regression that ignores public_enabled must flip P3 to FAIL."""
    from clipos_api import publishing

    original = publishing.evaluate_publish_policy

    def leaky(policy, visibility, operating_mode, account_approved=False):
        if str(getattr(operating_mode, "value", operating_mode)) in ("OBSERVE", "DRAFT"):
            return False, f"mode_{operating_mode}_denies_publishing"
        return True, "allowed"

    monkeypatch.setattr(publishing, "evaluate_publish_policy", leaky)
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")

    failing = {i["check_id"] for i in report["invariants"] if i["status"] == "FAIL"}
    assert "P3" in failing
    assert report["summary"]["verdict"] == "FAIL"
    monkeypatch.setattr(publishing, "evaluate_publish_policy", original)


def test_kill_switch_invariant_fails_when_switch_is_ignored(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regression that ignores the kill switch must flip P5 to FAIL."""
    from clipos_api import publishing

    original = publishing.evaluate_publish_policy

    def leaky(policy, visibility, operating_mode, account_approved=False):
        allowed, reason = original(policy, visibility, operating_mode, account_approved)
        if reason == "kill_switch_active":
            return True, "allowed"
        return allowed, reason

    monkeypatch.setattr(publishing, "evaluate_publish_policy", leaky)
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")

    failing = {i["check_id"] for i in report["invariants"] if i["status"] == "FAIL"}
    assert "P5" in failing
    monkeypatch.setattr(publishing, "evaluate_publish_policy", original)


def test_a_required_step_failure_is_reported(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a required stage cannot run, the report must say so and FAIL."""
    harness = _load_harness()
    original = harness.walk_pipeline

    def broken(walk):
        original(walk)
        walk.step("experiments", "forced failure", "GET", "/health", expect=(999,))

    monkeypatch.setattr(harness, "walk_pipeline", broken)
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")

    # The forced failure is a required step, so the verdict must FAIL and the
    # report must name the pipeline error.
    assert report["summary"]["steps_failed"] >= 1
    assert report["summary"]["verdict"] == "FAIL"


# ---------------------------------------------------------------------------
# Client transport
# ---------------------------------------------------------------------------


def test_client_records_every_body_seen(live_client: TestClient) -> None:
    transport = _TransportClient(live_client)
    transport.request("GET", "/health")
    assert len(transport.seen) == 1
    assert "ok" in transport.seen[0]
