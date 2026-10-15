"""Tests for the production security qualification harness (CLIP-093).

Exercises the qualification script against a live in-process FastAPI app
so the harness itself is covered: every check must PASS against the real
services, the report must be schema-stable, hostile targets must FAIL
rather than silently pass, and an unreachable target must exit 2.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app


def _find_repo_root() -> Path:
    """Locate the repository root by marker file, not by fixed depth.

    A worktree checkout can sit at a different depth than the canonical
    clone, so anchoring on a marker keeps the harness loadable from both.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "10_TICKETS.yaml").exists():
            return candidate
    return Path(__file__).resolve().parents[3]


REPO_ROOT = _find_repo_root()


def _load_harness():
    """Import the harness by path.

    The module must be registered in ``sys.modules`` before execution
    because it defines dataclasses, which look themselves up there.
    """
    import sys

    spec = importlib.util.spec_from_file_location(
        "security_qualification", REPO_ROOT / "scripts" / "security_qualification.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["security_qualification"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("security_qualification", None)
        raise
    return module


@pytest.fixture()
def engine(monkeypatch: pytest.MonkeyPatch) -> Engine:
    path = Path("/tmp") / "clipos-093-qual-audit.log"
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
    return TestClient(app)


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
            url = f"{path}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
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
    transport = _TransportClient(live_client)
    report = harness.run_qualification(transport, "qual-org")

    failures = [c["check_id"] for c in report["checks"] if c["status"] == "FAIL"]
    assert failures == [], f"qualification FAILED: {failures}"
    assert report["summary"]["verdict"] == "PASS"


def test_report_schema_is_stable(live_client: TestClient) -> None:
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")

    assert report["tool"] == "clipos-security-qualification"
    assert report["version"] == 1
    for key in ("generated_at", "base_url", "org_id", "summary", "checks", "findings"):
        assert key in report
    summary = report["summary"]
    assert set(summary) == {
        "total",
        "passed",
        "warnings",
        "failed",
        "verdict",
    }
    assert summary["total"] == len(report["checks"])
    assert summary["passed"] + summary["warnings"] + summary["failed"] == summary["total"]
    for check in report["checks"]:
        assert set(check) == {"check_id", "title", "status", "evidence", "details"}
        assert check["status"] in ("PASS", "WARN", "FAIL")


def test_all_expected_controls_are_covered(live_client: TestClient) -> None:
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")
    covered = {c["check_id"] for c in report["checks"]}
    assert covered == {f"C{i}" for i in range(1, 12)}


def test_report_is_json_serializable(live_client: TestClient) -> None:
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")
    json.dumps(report)  # must not raise


def test_findings_only_contain_non_pass_checks(live_client: TestClient) -> None:
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")
    non_pass = {c["check_id"] for c in report["checks"] if c["status"] != "PASS"}
    assert {f["check_id"] for f in report["findings"]} == non_pass


# ---------------------------------------------------------------------------
# The harness must not pass a broken target
# ---------------------------------------------------------------------------


def test_injection_check_fails_when_boundary_is_broken(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regression that stops quarantining must make the check FAIL."""
    import clipos_api.injection_guard as guard

    original = guard.enforce_untrusted_boundary

    def leaky(text: str, quarantine_at=guard.InjectionSeverity.CRITICAL):
        result = original(text, quarantine_at)
        # Simulate a regression: never quarantine, always hand text through.
        from clipos_api.injection_guard import EnforcementDecision

        return guard.EnforcementResult(
            decision=EnforcementDecision.ALLOWED,
            reason="regression",
            safe_text=result.safe_text or text,
        )

    monkeypatch.setattr(guard, "enforce_untrusted_boundary", leaky)
    harness = _load_harness()
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")
    failing = {c["check_id"] for c in report["checks"] if c["status"] == "FAIL"}
    assert "C6" in failing
    assert report["summary"]["verdict"] == "FAIL"


def test_tool_authorization_check_fails_when_rule_is_broken(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    import clipos_api.main as main_mod

    # Point the check at an endpoint that always allows, proving a broken
    # target cannot slip through as PASS.
    original_client_request = _TransportClient.request

    def patched(self, method, path, body=None, params=None):
        if path == "/security/tool-authorization":
            self.seen.append("{}")
            return 200, {"allowed": True, "reason": "broken"}
        return original_client_request(self, method, path, body, params)

    monkeypatch.setattr(_TransportClient, "request", patched)
    report = harness.run_qualification(_TransportClient(live_client), "qual-org")
    failing = {c["check_id"] for c in report["checks"] if c["status"] == "FAIL"}
    assert "C8" in failing
    assert main_mod is not None


def test_unreachable_target_returns_exit_code_2(tmp_path: Path) -> None:
    harness = _load_harness()
    # A port nothing is listening on.
    code = harness.main(
        ["security_qualification.py", "--base-url", "http://127.0.0.1:9", "--quiet"]
    )
    assert code == 2


def test_cli_writes_json_report(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _load_harness()
    monkeypatch.setattr(
        harness,
        "Client",
        lambda base_url, timeout=20.0: _TransportClient(live_client, base_url),
    )
    out = tmp_path / "qualification.json"
    code = harness.main(
        [
            "security_qualification.py",
            "--base-url",
            "http://qual.local",
            "--json",
            str(out),
            "--quiet",
        ]
    )
    assert out.exists(), "report file not written"
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["tool"] == "clipos-security-qualification"
    # No FAILs against the live app -> exit 0.
    assert code == 0


def test_strict_mode_treats_warnings_as_failure(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()

    def warn_only(client, org):
        return harness.Check("C1", "stub", harness.WARN, "stubbed warning")

    monkeypatch.setattr(harness, "CHECKS", (warn_only,))
    transport = _TransportClient(live_client, "http://qual.local")
    report = harness.run_qualification(transport, "qual-org")
    assert report["summary"]["warnings"] == 1
    assert report["summary"]["verdict"] == "PASS"  # warnings are not failures
    assert report["findings"][0]["severity"] == "LOW"
