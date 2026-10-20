"""Tests for autonomy evidence (CLIP-101).

The pure assembler is driven with synthetic ledgers (including deliberate
contradictions so a dishonest claim FAILs rather than passes), and the CLI
is driven against the repository's real build record.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from clipos_api.autonomy_evidence import (
    EXPECTED_HUMAN_GATES,
    QUALIFICATION_HARNESSES,
    BuildLedgerInput,
    EvidenceFinding,
    HarnessInput,
    OperatingPostureInput,
    Severity,
    assemble_evidence,
)


def _find_repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "10_TICKETS.yaml").exists():
            return candidate
    return Path(__file__).resolve().parents[3]


REPO_ROOT = _find_repo_root()


def _ledger(**overrides: object) -> BuildLedgerInput:
    payload: dict[str, object] = {
        "completed_ticket_ids": [f"CLIP-{i:03d}" for i in range(1, 6)],
        "tickets_completed": 5,
        "tickets_total": 10,
        "audit_covered": 5,
        "last_evidence_ticket": "CLIP-005",
        "evidence_entry_count": 5,
        "completed_milestones": ["M0", "M1"],
        "current_milestone": "M2",
        "current_ticket": "CLIP-006",
    }
    payload.update(overrides)
    return BuildLedgerInput(**payload)  # type: ignore[arg-type]


def _posture(**overrides: object) -> OperatingPostureInput:
    payload: dict[str, object] = {
        "operating_mode": "DRAFT",
        "public_publishing": False,
        "production_deploy": False,
        "human_gates": {gate: False for gate in EXPECTED_HUMAN_GATES},
        "autonomous_build": True,
    }
    payload.update(overrides)
    return OperatingPostureInput(**payload)  # type: ignore[arg-type]


def _harnesses(**overrides: object) -> HarnessInput:
    payload: dict[str, object] = {
        "present": sorted(QUALIFICATION_HARNESSES),
        "missing": [],
    }
    payload.update(overrides)
    return HarnessInput(**payload)  # type: ignore[arg-type]


def _by_id(report, check_id: str) -> list[EvidenceFinding]:
    return [f for f in report.findings if f.check_id == check_id]


# ---------------------------------------------------------------------------
# Ledger integrity
# ---------------------------------------------------------------------------


def test_consistent_ledger_has_no_failures() -> None:
    report = assemble_evidence(_ledger(), _posture(), _harnesses())
    fails = [f.check_id for f in report.findings if f.severity is Severity.FAIL]
    assert fails == []
    assert report.verdict == "PASS"


def test_counter_mismatch_fails() -> None:
    report = assemble_evidence(_ledger(tickets_completed=9), _posture(), _harnesses())
    e1 = _by_id(report, "E1")
    assert any(f.severity is Severity.FAIL for f in e1)
    assert "ledger" in " ".join(f.detail for f in e1)


def test_duplicate_ticket_ids_fail() -> None:
    ids = ["CLIP-001", "CLIP-001", "CLIP-002"]
    report = assemble_evidence(
        _ledger(
            completed_ticket_ids=ids,
            tickets_completed=3,
            audit_covered=3,
            last_evidence_ticket="CLIP-002",
        ),
        _posture(),
        _harnesses(),
    )
    e1 = _by_id(report, "E1")
    assert any("duplicated" in f.detail for f in e1 if f.severity is Severity.FAIL)
    assert report.verdict == "FAIL"


def test_completed_exceeding_total_fails() -> None:
    report = assemble_evidence(
        _ledger(tickets_total=3, tickets_completed=5), _posture(), _harnesses()
    )
    assert any("exceeds total" in f.detail for f in report.findings)


def test_audit_coverage_mismatch_fails() -> None:
    report = assemble_evidence(_ledger(audit_covered=4), _posture(), _harnesses())
    e2 = _by_id(report, "E2")
    assert any(f.severity is Severity.FAIL for f in e2)
    assert "4 of 5" in " ".join(f.detail for f in e2)


def test_last_evidence_naming_an_incomplete_ticket_fails() -> None:
    report = assemble_evidence(_ledger(last_evidence_ticket="CLIP-999"), _posture(), _harnesses())
    e3 = _by_id(report, "E3")
    assert any(f.severity is Severity.FAIL for f in e3)


def test_empty_evidence_trail_fails() -> None:
    report = assemble_evidence(
        _ledger(last_evidence_ticket=None, evidence_entry_count=0),
        _posture(),
        _harnesses(),
    )
    e3 = _by_id(report, "E3")
    assert any(f.severity is Severity.FAIL for f in e3)


def test_current_milestone_already_completed_fails() -> None:
    report = assemble_evidence(
        _ledger(completed_milestones=["M0", "M1", "M2"], current_milestone="M2"),
        _posture(),
        _harnesses(),
    )
    e4 = _by_id(report, "E4")
    assert any(f.severity is Severity.FAIL for f in e4)


# ---------------------------------------------------------------------------
# Operating posture
# ---------------------------------------------------------------------------


def test_unknown_operating_mode_fails() -> None:
    report = assemble_evidence(_ledger(), _posture(operating_mode="WILD"), _harnesses())
    e5 = _by_id(report, "E5")
    assert any(f.severity is Severity.FAIL for f in e5)


def test_every_prd_mode_is_accepted() -> None:
    for mode in ("OBSERVE", "DRAFT", "PRIVATE_TEST", "GUARDED_PUBLIC", "AUTONOMOUS_PUBLIC"):
        report = assemble_evidence(_ledger(), _posture(operating_mode=mode), _harnesses())
        assert all(f.check_id != "E5" or f.severity is not Severity.FAIL for f in report.findings)


def test_public_publishing_without_owner_gate_is_a_contradiction() -> None:
    """The core honesty rule: public operation requires the owner's approval."""
    report = assemble_evidence(
        _ledger(),
        _posture(public_publishing=True, human_gates={g: False for g in EXPECTED_HUMAN_GATES}),
        _harnesses(),
    )
    e6 = _by_id(report, "E6")
    assert any(f.severity is Severity.FAIL for f in e6)
    assert report.verdict == "FAIL"


def test_public_publishing_with_open_gate_is_consistent() -> None:
    gates = {g: False for g in EXPECTED_HUMAN_GATES}
    gates["first_public_post"] = True
    report = assemble_evidence(
        _ledger(), _posture(public_publishing=True, human_gates=gates), _harnesses()
    )
    e6 = _by_id(report, "E6")
    assert all(f.severity is not Severity.FAIL for f in e6)


def test_production_deploy_warns() -> None:
    report = assemble_evidence(_ledger(), _posture(production_deploy=True), _harnesses())
    e6 = _by_id(report, "E6")
    assert any(f.severity is Severity.WARN for f in e6)
    # A warning does not flip the verdict by itself.
    assert report.verdict == "PASS"


# ---------------------------------------------------------------------------
# Human gates and harnesses
# ---------------------------------------------------------------------------


def test_missing_gate_declaration_fails() -> None:
    gates = {g: False for g in EXPECTED_HUMAN_GATES}
    del gates["meta_real_account"]
    report = assemble_evidence(_ledger(), _posture(human_gates=gates), _harnesses())
    e7 = _by_id(report, "E7")
    assert any(f.severity is Severity.FAIL for f in e7)
    assert "meta_real_account" in " ".join(f.detail for f in e7)


def test_open_gates_are_reported_not_penalised() -> None:
    """Gates closed is the normal posture; an open gate is a fact, not a fault."""
    gates = {g: False for g in EXPECTED_HUMAN_GATES}
    gates["youtube_real_account"] = True
    report = assemble_evidence(_ledger(), _posture(human_gates=gates), _harnesses())
    e7 = _by_id(report, "E7")
    assert all(f.severity is Severity.INFO for f in e7)
    assert any("youtube_real_account" in f.detail for f in e7)


def test_missing_harness_fails() -> None:
    report = assemble_evidence(
        _ledger(),
        _posture(),
        _harnesses(present=[], missing=sorted(QUALIFICATION_HARNESSES)),
    )
    e8 = _by_id(report, "E8")
    assert any(f.severity is Severity.FAIL for f in e8)
    assert report.verdict == "FAIL"


def test_report_is_json_serializable() -> None:
    report = assemble_evidence(_ledger(), _posture(), _harnesses())
    json.dumps(report.model_dump(mode="json"))


def test_summary_counts_add_up() -> None:
    report = assemble_evidence(_ledger(), _posture(production_deploy=True), _harnesses())
    summary = report.summary
    assert summary["failed"] + summary["warnings"] + summary["info"] == summary["total"]


# ---------------------------------------------------------------------------
# The CLI against the repository's real build record
# ---------------------------------------------------------------------------


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "autonomy_evidence_cli", REPO_ROOT / "scripts" / "autonomy_evidence.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["autonomy_evidence_cli"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("autonomy_evidence_cli", None)
        raise
    return module


def test_cli_gathers_the_real_build_record() -> None:
    cli = _load_cli()
    report, state = cli.gather(REPO_ROOT)

    assert report.tool == "clipos-autonomy-evidence"
    assert report.version == 1
    assert state["current_ticket"]
    # The real ledger is complete and audit-covered, so the report must pass.
    assert report.verdict == "PASS", json.dumps(
        [f.detail for f in report.findings if f.severity == "FAIL"], indent=2
    )
    ids = {f.check_id for f in report.findings}
    assert {f"E{i}" for i in range(1, 10)} <= ids


def test_cli_report_is_json_serializable_and_schema_stable() -> None:
    cli = _load_cli()
    report, _state = cli.gather(REPO_ROOT)
    payload = report.model_dump(mode="json")
    json.dumps(payload)
    for key in ("tool", "version", "verdict", "findings", "summary"):
        assert key in payload
    for finding in payload["findings"]:
        assert set(finding) == {"check_id", "title", "severity", "detail"}


def test_cli_main_exits_zero_on_the_real_repo() -> None:
    cli = _load_cli()
    assert cli.main(["autonomy_evidence.py", "--repo", str(REPO_ROOT), "--quiet"]) == 0


def test_cli_main_exits_one_when_the_ledger_lies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A doctored build record must fail, proving the CLI is not a rubber stamp."""
    state = json.loads((REPO_ROOT / "BUILD_STATE.json").read_text(encoding="utf-8-sig"))
    state["tickets_completed"] = 9999
    doctored = tmp_path / "BUILD_STATE.json"
    doctored.write_text(json.dumps(state), encoding="utf-8")

    cli = _load_cli()

    monkeypatch.setattr(cli, "_read_build_state", lambda root: json.loads(doctored.read_text()))
    report, _state = cli.gather(tmp_path)
    assert report.verdict == "FAIL"


def test_cli_main_exits_two_when_the_record_is_missing(tmp_path: Path) -> None:
    cli = _load_cli()
    empty = tmp_path / "nowhere"
    empty.mkdir()
    assert cli.main(["autonomy_evidence.py", "--repo", str(empty), "--quiet"]) == 2
