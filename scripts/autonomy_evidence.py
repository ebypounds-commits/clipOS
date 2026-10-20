#!/usr/bin/env python3
"""Autonomy evidence report (CLIP-101).

Assembles the machine-checkable evidence pack M24's exit gate asks for: it
reads the build's own record (BUILD_STATE.json, STATE_AUDIT.jsonl), verifies
the claims against each other, inventories the qualification harnesses, and
probes the CI record when the repository has one.

The pure rules live in `clipos_api.autonomy_evidence`; this CLI gathers the
inputs. Every check reports what it observed Ã¢â‚¬â€ an unreachable check is
reported as UNKNOWN and flagged, never silently treated as healthy.

Usage:
  python3 scripts/autonomy_evidence.py [--repo ROOT] [--json PATH]
                                       [--strict] [--quiet]

Exit codes:
  0  verdict PASS
  1  verdict FAIL (or any warning under --strict)
  2  the build record could not be read
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[1] / "services" / "api"
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from clipos_api.autonomy_evidence import (
    QUALIFICATION_HARNESSES,
    BuildLedgerInput,
    EvidenceFinding,
    HarnessInput,
    OperatingPostureInput,
    Severity,
    assemble_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_build_state(root: Path) -> dict:
    return json.loads((root / "BUILD_STATE.json").read_text(encoding="utf-8-sig"))


def _audit_covered(root: Path) -> int | None:
    """Covered-ticket count from state_audit (None when it cannot run)."""
    try:
        result = subprocess.run(
            [sys.executable, str(root / "scripts" / "state_audit.py"), "verify"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"audit:\s*(\d+)(?:/(\d+))?\s+covered", result.stdout)
    if match:
        return int(match.group(1))
    for token in result.stdout.replace(",", " ").split():
        if token.isdigit():
            return int(token)
    return None


def _harness_inventory(root: Path) -> HarnessInput:
    present, missing = [], []
    for name in QUALIFICATION_HARNESSES:
        candidates = [root / "scripts" / name]
        if name.endswith(".sh"):
            candidates.append(root / name)
        (present if any(p.exists() for p in candidates) else missing).append(name)
    return HarnessInput(present=sorted(present), missing=sorted(missing))


def _ci_finding(root: Path) -> EvidenceFinding:
    """Probe the CI record branch; report WARN/UNKNOWN when unavailable."""
    try:
        fetch = subprocess.run(
            ["git", "fetch", "origin", "ci-results"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        show = subprocess.run(
            ["git", "show", "origin/ci-results:last_run.txt"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return EvidenceFinding(
            check_id="E9",
            title="CI record is reachable",
            severity=Severity.WARN,
            detail="git probe timed out; CI state UNKNOWN",
        )
    if fetch.returncode != 0:
        return EvidenceFinding(
            check_id="E9",
            title="CI record is reachable",
            severity=Severity.WARN,
            detail="could not fetch origin/ci-results (offline or no remote CI)",
        )
    fields: dict[str, str] = {}
    for line in show.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    sha, status = fields.get("sha", ""), fields.get("status", "")
    if not sha or not status:
        return EvidenceFinding(
            check_id="E9",
            title="CI record is readable",
            severity=Severity.WARN,
            detail="origin/ci-results has no parsable last_run.txt; CI state UNKNOWN",
        )
    if head.startswith(sha[: len(head)]) or sha.startswith(head[: len(sha)]):
        return EvidenceFinding(
            check_id="E9",
            title="latest CI run covers the current head",
            severity=Severity.INFO if status == "success" else Severity.FAIL,
            detail=f"run={fields.get('run')} sha={sha[:12]} status={status}",
        )
    return EvidenceFinding(
        check_id="E9",
        title="latest CI run covers the current head",
        severity=Severity.WARN,
        detail=(
            f"last recorded run sha={sha[:12]} status={status}; HEAD={head[:12]} Ã¢â‚¬â€ "
            "the run for the current head may still be pending"
        ),
    )


def _finalize(report, extra: list[EvidenceFinding]):
    findings = list(report.findings) + extra
    failed = sum(1 for f in findings if f.severity is Severity.FAIL)
    warned = sum(1 for f in findings if f.severity is Severity.WARN)
    info = sum(1 for f in findings if f.severity is Severity.INFO)
    return replace(
        report,
        findings=findings,
        verdict="FAIL" if failed else "PASS",
        summary={
            **report.summary,
            "total": len(findings),
            "failed": failed,
            "warnings": warned,
            "info": info,
        },
    )


def gather(root: Path):
    state = _read_build_state(root)
    covered = _audit_covered(root)
    ledger = BuildLedgerInput(
        completed_ticket_ids=list(state.get("completed_tickets", [])),
        tickets_completed=int(state.get("tickets_completed", 0)),
        tickets_total=int(state.get("tickets_total", 0)),
        audit_covered=covered if covered is not None else -1,
        last_evidence_ticket=(state.get("last_evidence") or {}).get("ticket"),
        evidence_entry_count=len(state.get("evidence", [])),
        completed_milestones=list(state.get("completed_milestones", [])),
        current_milestone=str(state.get("current_milestone", "")),
        current_ticket=str(state.get("current_ticket", "")),
    )
    posture = OperatingPostureInput(
        operating_mode=str(state.get("operating_mode", "")),
        public_publishing=bool(state.get("public_publishing", False)),
        production_deploy=bool(state.get("production_deploy", False)),
        human_gates=dict(state.get("human_gates", {})),
        autonomous_build=bool(state.get("autonomous_build", True)),
    )
    report = assemble_evidence(ledger, posture, _harness_inventory(root))

    extra: list[EvidenceFinding] = []
    if covered is None:
        extra.append(
            EvidenceFinding(
                check_id="E2",
                title="audit trail covers every completed ticket",
                severity=Severity.WARN,
                detail="state_audit.py could not run; coverage is UNKNOWN",
            )
        )
    extra.append(_ci_finding(root))
    return _finalize(report, extra), state


def render(report) -> None:
    print("ClipOS autonomy evidence report")
    print()
    for finding in report.findings:
        print(f"[{finding.severity.value:5}] {finding.check_id} {finding.title}")
        print(f"        {finding.detail}")
    print()
    summary = report.summary
    print(
        f"VERDICT: {report.verdict} "
        f"({summary.get('total', 0)} checks: {summary.get('failed', 0)} failed, "
        f"{summary.get('warnings', 0)} warnings, {summary.get('info', 0)} info)"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--json", default="", help="write the report JSON here")
    parser.add_argument("--strict", action="store_true", help="warnings fail too")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv[1:])

    root = Path(args.repo).resolve()
    try:
        report, _state = gather(root)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"autonomy evidence could not run: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        render(report)
    if args.json:
        Path(args.json).write_text(
            json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nreport written to {args.json}")

    if report.verdict == "FAIL":
        return 1
    if args.strict and report.summary.get("warnings", 0):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
