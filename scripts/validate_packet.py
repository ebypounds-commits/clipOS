#!/usr/bin/env python3
"""Validate Codex task packets (CLIP-005 runner conventions).

Stdlib-only. Checks required headings and lease keys; rejects empty sections.

Usage:
  validate_packet.py <path> [<path>...]

Exit 0 prints "packet: valid (<path>)" per file; exit 1 prints every missing
piece.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REQUIRED_HEADINGS: list[tuple[str, bool]] = [
    ("## Lease", True),  # exact
    ("## Objective", True),  # exact
    ("Acceptance", False),  # any "## ...Acceptance..." heading
    ("## Completion output", True),  # exact
]
REQUIRED_LEASE_KEYS = ("ticket_id", "orchestrator_run_id")


def _sections(text: str) -> list[tuple[str, str]]:
    """Return (heading, body) pairs in document order."""
    parts = re.split(r"^(?=#{1,6} )", text, flags=re.MULTILINE)
    out: list[tuple[str, str]] = []
    for part in parts:
        if not part.strip():
            continue
        lines = part.splitlines()
        out.append((lines[0].strip(), "\n".join(lines[1:])))
    return out


def _extract_lease(body: str) -> dict[str, str]:
    lease: dict[str, str] = {}
    for line in body.splitlines():
        match = re.match(r"^-\s*([A-Za-z0-9_-]+)\s*:(.*)$", line)
        if match:
            lease[match.group(1)] = match.group(2).strip()
    return lease


def validate_packet(path: Path) -> list[str]:
    """Validate one packet file; return a list of problems (empty = valid)."""
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path}: unreadable ({exc})"]
    sections = _sections(text)
    headings = [heading for heading, _ in sections]

    if not headings or not headings[0].startswith("# Codex Task Packet"):
        problems.append("missing top heading: '# Codex Task Packet'")

    for required, exact in REQUIRED_HEADINGS:
        if exact:
            if required not in headings:
                problems.append(f"missing required heading: {required!r}")
        elif not any("Acceptance" in heading for heading in headings):
            problems.append("missing required Acceptance section heading")

    lease_body = next((body for heading, body in sections if heading == "## Lease"), "")
    if lease_body:
        lease = _extract_lease(lease_body)
        for key in REQUIRED_LEASE_KEYS:
            if key not in lease:
                problems.append(f"missing lease key: {key}")
    for heading, body in sections:
        if heading == "## Lease" or heading.startswith("# Codex Task Packet"):
            continue
        if not body.strip():
            problems.append(f"empty section: {heading}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: validate_packet.py <path> [<path>...]", file=sys.stderr)
        return 2
    all_problems: list[str] = []
    for arg in argv[1:]:
        path = Path(arg)
        problems = validate_packet(path)
        if problems:
            all_problems.extend(f"{path}: {p}" for p in problems)
        else:
            print(f"packet: valid ({path})")
    for problem in all_problems:
        print(problem, file=sys.stderr)
    return 1 if all_problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
