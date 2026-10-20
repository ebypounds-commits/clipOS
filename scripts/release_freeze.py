#!/usr/bin/env python3
"""Release freeze validation (CLIP-102).

Validates that the repository actually is the v1 release its manifest
claims: package version agreement, the complete ticket ledger, every
milestone M0..M24 done, the qualification harnesses present, the human
gates documented, and the post-launch backlog in place. Emits a versioned
JSON report; suitable for CI and for the release evidence pack.

The manifest is `release/v1.manifest.json`. This script is stdlib-only on
purpose (see CLIP-094/CLIP-101): it must run anywhere python3 runs.

Usage:
  python3 scripts/release_freeze.py [--repo ROOT] [--json PATH] [--quiet]

Exit codes:
  0  the freeze is valid
  1  at least one freeze check failed
  2  a required file could not be read or parsed
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class FreezeFinding:
    check_id: str
    title: str
    severity: str
    detail: str


@dataclass(frozen=True)
class FreezeReport:
    verdict: str
    release: str
    head_sha: str
    findings: list[FreezeFinding] = field(default_factory=list)
    tool: str = "clipos-release-freeze"
    version: int = 1
    generated_at: str = field(
        default_factory=lambda: datetime.now(UTC).replace(microsecond=0).isoformat()
    )

    def to_dict(self) -> dict:
        data = asdict(self)
        return data


def _head_sha(root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=60, check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _pyproject_version(root: Path) -> str | None:
    text = (root / "services" / "api" / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    return match.group(1) if match else None


def _finding(check_id, title, severity, detail):
    return FreezeFinding(check_id=check_id, title=title, severity=severity, detail=detail)


def _finalize(report: FreezeReport, extra: list[FreezeFinding]) -> FreezeReport:
    findings = list(report.findings) + extra
    failed = sum(1 for f in findings if f.severity == "FAIL")
    return FreezeReport(
        verdict="FAIL" if failed else "PASS",
        release=report.release,
        head_sha=report.head_sha,
        findings=findings,
        tool=report.tool,
        version=report.version,
        generated_at=report.generated_at,
    )


def validate(root: Path) -> FreezeReport:
    # The manifest comes from the repo under validation, never from this
    # script's own checkout — otherwise --repo mutations are invisible.
    manifest = json.loads(
        (root / "release" / "v1.manifest.json").read_text(encoding="utf-8")
    )
    report = FreezeReport(verdict="PASS", release=manifest.get("release", "?"),
                          head_sha=_head_sha(root))
    extra: list[FreezeFinding] = []

    # F1 manifest shape
    required_keys = ("release", "api_package", "api_package_version", "requires",
                     "deferred_to_post_launch", "backlog")
    missing_keys = [k for k in required_keys if k not in manifest]
    if missing_keys:
        extra.append(_finding(
            "F1", "manifest is complete", "FAIL",
            f"missing keys: {', '.join(missing_keys)}",
        ))
    else:
        extra.append(_finding(
            "F1", "manifest is complete", "INFO",
            f"release={manifest['release']} package={manifest['api_package']}",
        ))

    # F2 version agreement: pyproject == manifest
    py_version = _pyproject_version(root)
    if py_version == manifest.get("api_package_version") == manifest.get("release"):
        extra.append(_finding(
            "F2", "package version matches the manifest", "INFO",
            f"clipos-api {py_version}",
        ))
    else:
        extra.append(_finding(
            "F2", "package version matches the manifest", "FAIL",
            f"pyproject={py_version} manifest={manifest.get('api_package_version')} "
            f"release={manifest.get('release')}",
        ))

    # F3 ledger complete
    state = json.loads(
        (root / "BUILD_STATE.json").read_text(encoding="utf-8-sig")
    )
    requires = manifest.get("requires", {})
    completed = state.get("completed_tickets", [])
    total = state.get("tickets_total")
    expected_total = requires.get("tickets_total")
    ledger_ok = (
        total == expected_total
        and state.get("tickets_completed") == len(completed) == expected_total
    )
    if ledger_ok:
        extra.append(_finding(
            "F3", "ticket ledger is complete", "INFO",
            f"{len(completed)}/{total} tickets completed",
        ))
    else:
        extra.append(_finding(
            "F3", "ticket ledger is complete", "FAIL",
            f"completed={state.get('tickets_completed')} ids={len(completed)} "
            f"total={total} manifest_total={expected_total}",
        ))

    # F4 milestones
    milestones_expected = set(manifest.get("milestones_expected", []))
    milestones_done = set(state.get("completed_milestones", []))
    missing_milestones = sorted(milestones_expected - milestones_done)
    if missing_milestones:
        extra.append(_finding(
            "F4", "every milestone M0..M24 is complete", "FAIL",
            f"not complete: {', '.join(missing_milestones)}",
        ))
    else:
        extra.append(_finding(
            "F4", "every milestone M0..M24 is complete", "INFO",
            f"{len(milestones_done)} milestones recorded",
        ))

    # F5 harnesses present
    missing_harnesses = [
        rel for rel in requires.get("qualification_harnesses", [])
        if not (root / rel).exists()
    ]
    if missing_harnesses:
        extra.append(_finding(
            "F5", "qualification harnesses are present", "FAIL",
            f"missing: {', '.join(missing_harnesses)}",
        ))
    else:
        extra.append(_finding(
            "F5", "qualification harnesses are present", "INFO",
            f"{len(requires.get('qualification_harnesses', []))} harnesses",
        ))

    # F6 human gates documented and honestly deferred
    documented = set(state.get("human_gates", {}))
    gates_expected = set(requires.get("human_gates_documented", []))
    deferred = set(
        manifest.get("deferred_to_post_launch", {}).get("human_gates", [])
    )
    if documented != gates_expected:
        extra.append(_finding(
            "F6", "human gates are documented", "FAIL",
            f"BUILD_STATE declares {sorted(documented)}; manifest expects "
            f"{sorted(gates_expected)}",
        ))
    elif deferred != gates_expected:
        extra.append(_finding(
            "F6", "human gates are documented", "FAIL",
            "deferred_to_post_launch must list every gate the freeze does not "
            f"include (expected {sorted(gates_expected)}, got {sorted(deferred)})",
        ))
    else:
        extra.append(_finding(
            "F6", "human gates are documented and deferred honestly", "INFO",
            f"{len(documented)} gates declared, all listed as post-launch",
        ))

    # F7 backlog present
    backlog = root / str(manifest.get("backlog", "docs/BACKLOG.md"))
    if backlog.is_file() and backlog.stat().st_size > 0:
        extra.append(_finding(
            "F7", "post-launch backlog exists", "INFO", str(backlog.relative_to(root))
        ))
    else:
        extra.append(_finding(
            "F7", "post-launch backlog exists", "FAIL",
            f"missing or empty: {backlog}",
        ))

    return _finalize(report, extra)


def render(report: FreezeReport) -> None:
    print(f"ClipOS release freeze — v{report.release} (HEAD {report.head_sha[:12]})")
    print()
    for finding in report.findings:
        print(f"[{finding.severity:4}] {finding.check_id} {finding.title}")
        print(f"        {finding.detail}")
    failed = sum(1 for f in report.findings if f.severity == "FAIL")
    print()
    print(f"VERDICT: {report.verdict} ({len(report.findings)} checks, {failed} failed)")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--json", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv[1:])

    root = Path(args.repo).resolve()
    try:
        report = validate(root)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"release freeze could not run: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        render(report)
    if args.json:
        Path(args.json).write_text(
            json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nreport written to {args.json}")
    return 0 if report.verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
