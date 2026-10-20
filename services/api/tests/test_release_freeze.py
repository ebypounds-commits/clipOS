"""Tests for the v1 release freeze validator (CLIP-102).

The real repository must validate PASS end to end, and every individual
check must be proven able to FAIL — a freeze validator that cannot reject a
broken release is decoration.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest


def _find_repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "10_TICKETS.yaml").exists():
            return candidate
    return Path(__file__).resolve().parents[3]


REPO_ROOT = _find_repo_root()


def _load():
    spec = importlib.util.spec_from_file_location(
        "release_freeze", REPO_ROOT / "scripts" / "release_freeze.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_freeze"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("release_freeze", None)
        raise
    return module


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A minimal but complete repo mirror for mutation testing."""
    (tmp_path / "release").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "services" / "api").mkdir(parents=True)
    for rel in ("scripts/security_qualification.py",
                "scripts/private_mode_qualification.py",
                "scripts/deploy_topology_check.py",
                "scripts/schema_drift.py",
                "scripts/autonomy_evidence.py",
                "scripts/infrastructure_health.sh"):
        (tmp_path / rel).write_text("#\n", encoding="utf-8")
    (tmp_path / "docs" / "BACKLOG.md").write_text("# backlog\n", encoding="utf-8")
    manifest = json.loads(
        (REPO_ROOT / "release" / "v1.manifest.json").read_text(encoding="utf-8")
    )
    (tmp_path / "release" / "v1.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (tmp_path / "services" / "api" / "pyproject.toml").write_text(
        '[project]\nname = "clipos-api"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    state = {
        "completed_tickets": [f"T{i}" for i in range(1, 109)],
        "tickets_completed": 108,
        "tickets_total": 108,
        "completed_milestones": [f"M{i}" for i in range(0, 25)],
        "human_gates": {g: False for g in
                        ("production_secrets", "youtube_real_account",
                         "tiktok_real_account", "meta_real_account",
                         "first_public_post")},
    }
    (tmp_path / "BUILD_STATE.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


def test_real_repository_freezes_pass() -> None:
    mod = _load()
    report = mod.validate(REPO_ROOT)
    assert report.verdict == "PASS", json.dumps(
        [f.detail for f in report.findings if f.severity == "FAIL"], indent=2
    )
    assert report.release == "1.0.0"
    # head_sha is best-effort evidence: the test user may lack git access to a
    # root-owned checkout (by design — build credentials are separate), so an
    # empty value is accepted here; when present it must be a real SHA.
    assert report.head_sha == "" or re.fullmatch(r"[0-9a-f]{40}", report.head_sha)


def test_real_manifest_declares_every_gate_as_deferred() -> None:
    manifest = json.loads(
        (REPO_ROOT / "release" / "v1.manifest.json").read_text(encoding="utf-8")
    )
    deferred = set(manifest["deferred_to_post_launch"]["human_gates"])
    assert deferred == set(manifest["requires"]["human_gates_documented"])


def test_real_freeze_report_is_json_serializable() -> None:
    mod = _load()
    payload = mod.validate(REPO_ROOT).to_dict()
    json.dumps(payload)
    assert payload["tool"] == "clipos-release-freeze"
    assert payload["version"] == 1


def test_manifest_missing_key_fails(repo: Path) -> None:
    mod = _load()
    path = repo / "release" / "v1.manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    del manifest["backlog"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    report = mod.validate(repo)
    assert any(f.check_id == "F1" and f.severity == "FAIL" for f in report.findings)


def test_version_mismatch_fails(repo: Path) -> None:
    mod = _load()
    (repo / "services" / "api" / "pyproject.toml").write_text(
        '[project]\nversion = "0.9.0"\n', encoding="utf-8"
    )
    report = mod.validate(repo)
    f2 = [f for f in report.findings if f.check_id == "F2"]
    assert any(f.severity == "FAIL" for f in f2)
    assert report.verdict == "FAIL"


def test_incomplete_ledger_fails(repo: Path) -> None:
    mod = _load()
    path = repo / "BUILD_STATE.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["tickets_completed"] = 107
    path.write_text(json.dumps(state), encoding="utf-8")
    report = mod.validate(repo)
    assert any(f.check_id == "F3" and f.severity == "FAIL" for f in report.findings)


def test_incomplete_milestone_fails(repo: Path) -> None:
    mod = _load()
    path = repo / "BUILD_STATE.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["completed_milestones"] = [f"M{i}" for i in range(0, 24)]  # no M24
    path.write_text(json.dumps(state), encoding="utf-8")
    report = mod.validate(repo)
    f4 = [f for f in report.findings if f.check_id == "F4"]
    assert any(f.severity == "FAIL" for f in f4)
    assert "M24" in " ".join(f.detail for f in f4)


def test_missing_harness_fails(repo: Path) -> None:
    mod = _load()
    (repo / "scripts" / "schema_drift.py").unlink()
    report = mod.validate(repo)
    assert any(f.check_id == "F5" and f.severity == "FAIL" for f in report.findings)


def test_gate_documentation_mismatch_fails(repo: Path) -> None:
    mod = _load()
    path = repo / "BUILD_STATE.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["human_gates"] = {"production_secrets": False}
    path.write_text(json.dumps(state), encoding="utf-8")
    report = mod.validate(repo)
    assert any(f.check_id == "F6" and f.severity == "FAIL" for f in report.findings)


def test_missing_backlog_fails(repo: Path) -> None:
    mod = _load()
    (repo / "docs" / "BACKLOG.md").unlink()
    report = mod.validate(repo)
    assert any(f.check_id == "F7" and f.severity == "FAIL" for f in report.findings)


def test_main_exits_zero_on_the_real_repo() -> None:
    mod = _load()
    assert mod.main(["release_freeze.py", "--repo", str(REPO_ROOT), "--quiet"]) == 0


def test_main_exits_two_when_the_record_is_missing(tmp_path: Path) -> None:
    mod = _load()
    empty = tmp_path / "nowhere"
    empty.mkdir()
    assert mod.main(["release_freeze.py", "--repo", str(empty), "--quiet"]) == 2
