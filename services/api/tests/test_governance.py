"""Governance tests: build-state schema, ADR records, architecture guard."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

spec = importlib.util.spec_from_file_location(
    "validate_state", REPO_ROOT / "scripts" / "validate_state.py"
)
validate_state_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validate_state_module)

guard_spec = importlib.util.spec_from_file_location(
    "architecture_guard", REPO_ROOT / "scripts" / "architecture_guard.py"
)
guard_module = importlib.util.module_from_spec(guard_spec)
assert guard_spec.loader is not None
guard_spec.loader.exec_module(guard_module)

FOUNDRING_ADRS = [
    "0001-modular-monolith-and-worker-processes.md",
    "0002-core-stack-selection.md",
    "0003-provider-registry-alias-policy.md",
    "0004-ci-gates-and-deferral-policy.md",
]


def _load_state_and_schema() -> tuple[dict, dict]:
    state = json.loads((REPO_ROOT / "BUILD_STATE.json").read_text(encoding="utf-8"))
    schema = json.loads(
        (REPO_ROOT / "schemas" / "build_state.schema.json").read_text(encoding="utf-8")
    )
    return state, schema


def test_build_state_schema_file_parses() -> None:
    schema = json.loads(
        (REPO_ROOT / "schemas" / "build_state.schema.json").read_text(encoding="utf-8")
    )
    assert schema["$schema"].endswith("draft-07/schema#")
    assert set(schema["required"]) >= {
        "current_ticket",
        "status",
        "completed_tickets",
        "milestone",
        "last_evidence",
        "next_action",
    }


def test_real_build_state_is_valid() -> None:
    state, schema = _load_state_and_schema()
    tickets = validate_state_module.ticket_ids()
    assert validate_state_module.validate_state(state, schema, tickets) == []


def test_status_enum_rejects_bogus_status() -> None:
    state, schema = _load_state_and_schema()
    state["status"] = "TOTALLY_BOGUS_STATUS"
    tickets = validate_state_module.ticket_ids()
    errors = validate_state_module.validate_state(state, schema, tickets)
    assert any("status" in error for error in errors)


def test_current_ticket_must_not_be_completed() -> None:
    state, schema = _load_state_and_schema()
    state["completed_tickets"] = state["completed_tickets"] + [state["current_ticket"]]
    errors = validate_state_module.validate_state(state, schema, validate_state_module.ticket_ids())
    assert any("completed_tickets" in error for error in errors)


def test_founding_adrs_exist_and_are_accepted() -> None:
    adr_dir = REPO_ROOT / "docs" / "adr"
    assert (adr_dir / "README.md").is_file()
    assert (adr_dir / "0000-template.md").is_file()
    for name in FOUNDRING_ADRS:
        text = (adr_dir / name).read_text(encoding="utf-8")
        assert "## Status" in text
        assert "Accepted" in text


def test_architecture_guard_passes_from_repo_root() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/architecture_guard.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_architecture_guard_fails_on_missing_adr() -> None:
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        fake_root = Path(tmp) / "repo"
        shutil.copytree(REPO_ROOT / "docs", fake_root / "docs")
        shutil.copytree(REPO_ROOT / "services", fake_root / "services")
        shutil.copytree(REPO_ROOT / ".github", fake_root / ".github")
        (fake_root / "BUILD_STATE.json").write_text("{}", encoding="utf-8")
        shutil.rmtree(fake_root / "docs" / "adr")
        failures = guard_module.run_guard(fake_root)
        assert any(name == "adr-set" for name, _ in failures)
