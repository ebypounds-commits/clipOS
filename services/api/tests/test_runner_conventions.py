"""Tests for CLIP-005 runner conventions tooling (stdlib-only validators)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import state_audit

ROOT = Path(__file__).resolve().parents[3]


def _run_validator(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "validate_packet.py"), *(str(p) for p in paths)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_validate_packet_accepts_template() -> None:
    result = _run_validator(ROOT / ".agent" / "CODEX_TASK_PACKET_TEMPLATE.md")
    assert result.returncode == 0, result.stderr
    assert "packet: valid" in result.stdout


def test_validate_packet_accepts_clip005_packet() -> None:
    result = _run_validator(ROOT / ".agent" / "task_packet_CLIP-005.md")
    assert result.returncode == 0, result.stderr


def test_validate_packet_rejects_missing_sections(tmp_path: Path) -> None:
    packet = tmp_path / "broken.md"
    packet.write_text(
        "# Codex Task Packet\n\n## Lease\n- ticket_id: X\n\n## Objective\nobj\n\n"
        "## Completion output\nout\n",
        encoding="utf-8",
    )
    result = _run_validator(packet)
    assert result.returncode == 1
    stderr = result.stderr
    assert "missing lease key: orchestrator_run_id" in stderr
    assert "missing required Acceptance section heading" in stderr


def test_validate_packet_rejects_empty_section(tmp_path: Path) -> None:
    packet = tmp_path / "empty.md"
    packet.write_text(
        "# Codex Task Packet\n\n## Lease\n- ticket_id: X\n- orchestrator_run_id: Y\n\n"
        "## Objective\n\n## Acceptance criteria\nok\n\n## Completion output\nout\n",
        encoding="utf-8",
    )
    result = _run_validator(packet)
    assert result.returncode == 1
    assert "empty section: ## Objective" in result.stderr


def test_state_audit_append_idempotent(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    assert state_audit.append(audit, "CLIP-999", "abc1234") == 0
    before = audit.read_text(encoding="utf-8")
    assert state_audit.append(audit, "CLIP-999", "def5678") == 0
    assert audit.read_text(encoding="utf-8") == before
    lines = [json.loads(line) for line in before.splitlines()]
    assert len(lines) == 1
    assert lines[0]["ticket"] == "CLIP-999"
    assert lines[0]["actor"] == "hermes-clipos-m1-001"
    assert "at" in lines[0]


def test_state_audit_verify_and_bootstrap(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    state = tmp_path / "BUILD_STATE.json"
    state.write_text(
        json.dumps(
            {
                "completed_tickets": ["CLIP-1", "CLIP-2"],
                "last_evidence": {
                    "ticket": "CLIP-2",
                    "facts": "landed commit 0123456789abcdef0123456789abcdef01234567",
                },
            }
        ),
        encoding="utf-8",
    )
    assert state_audit.verify(audit, state) == 1
    assert state_audit.bootstrap(audit, state) == 0
    entries = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    by_ticket = {e["ticket"]: e for e in entries}
    assert by_ticket["CLIP-2"]["commit_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert by_ticket["CLIP-1"]["commit_sha"] == "unknown"
    before = audit.read_text(encoding="utf-8")
    assert state_audit.bootstrap(audit, state) == 0
    assert audit.read_text(encoding="utf-8") == before
    assert state_audit.verify(audit, state) == 0


def test_state_audit_verify_cli_reports_coverage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = tmp_path / "audit.jsonl"
    state = tmp_path / "BUILD_STATE.json"
    state.write_text(json.dumps({"completed_tickets": ["CLIP-1"]}), encoding="utf-8")
    state_audit.append(audit, "CLIP-1", "abc1234")
    assert state_audit.verify(audit, state) == 0
    assert "audit: 1/1 covered" in capsys.readouterr().out
