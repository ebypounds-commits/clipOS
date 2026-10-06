#!/usr/bin/env python3
"""Append-only state-transition audit (CLIP-005).

Audit file: docs/reports/STATE_AUDIT.jsonl (JSON Lines, append-only; existing
lines are never rewritten). Importable functions take explicit paths; the CLI
honours STATE_AUDIT_PATH and BUILD_STATE_PATH overrides (used by tests).

Usage:
  state_audit.py append <ticket> <commit_sha>
  state_audit.py verify
  state_audit.py bootstrap
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT_PATH = ROOT / "docs" / "reports" / "STATE_AUDIT.jsonl"
DEFAULT_STATE_PATH = ROOT / "BUILD_STATE.json"
ACTOR = "hermes-clipos-m1-001"
SHA_RE = re.compile(r"(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}")


def audit_path() -> Path:
    return Path(os.environ.get("STATE_AUDIT_PATH", str(DEFAULT_AUDIT_PATH)))


def state_path() -> Path:
    return Path(os.environ.get("BUILD_STATE_PATH", str(DEFAULT_STATE_PATH)))


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    return entries


def _tickets_with_evidence(state_file: Path) -> list[tuple[str, str]]:
    state = json.loads(state_file.read_text(encoding="utf-8"))
    evidence = state.get("last_evidence", {})
    sha = "unknown"
    facts = str(evidence.get("facts", ""))
    match = SHA_RE.search(facts)
    if match:
        sha = match.group(0)
    result: list[tuple[str, str]] = []
    for ticket in state.get("completed_tickets", []):
        result.append(
            (str(ticket), sha if str(ticket) == evidence.get("ticket") else "unknown")
        )
    return result


def append(audit_file: Path, ticket: str, commit_sha: str, actor: str = ACTOR) -> int:
    """Append one entry; no-op (prints 'exists') if the ticket already has one."""
    entries = _load_entries(audit_file)
    if any(entry.get("ticket") == ticket for entry in entries):
        print("exists")
        return 0
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": _now_iso(),
        "ticket": ticket,
        "commit_sha": commit_sha,
        "actor": actor,
    }
    with audit_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    print("appended")
    return 0


def verify(audit_file: Path, state_file: Path) -> int:
    """Exit 0 only if every completed ticket has an audit entry."""
    tickets = [t for t, _ in _tickets_with_evidence(state_file)]
    covered = {entry.get("ticket") for entry in _load_entries(audit_file)}
    missing = [t for t in tickets if t not in covered]
    if missing:
        print("missing audit entries for: " + ", ".join(missing))
        return 1
    print(f"audit: {len(tickets)}/{len(tickets)} covered")
    return 0


def bootstrap(audit_file: Path, state_file: Path, actor: str = ACTOR) -> int:
    """Create entries for completed tickets lacking one (idempotent)."""
    for ticket, sha in _tickets_with_evidence(state_file):
        entries = _load_entries(audit_file)
        if any(entry.get("ticket") == ticket for entry in entries):
            continue
        audit_file.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": _now_iso(),
            "ticket": ticket,
            "commit_sha": sha,
            "actor": actor,
        }
        with audit_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        print(f"bootstrapped: {ticket}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: state_audit.py append <ticket> <commit_sha> | verify | bootstrap",
            file=sys.stderr,
        )
        return 2
    cmd = argv[1]
    if cmd == "append":
        if len(argv) != 4:
            print("usage: state_audit.py append <ticket> <commit_sha>", file=sys.stderr)
            return 2
        return append(audit_path(), argv[2], argv[3])
    if cmd == "verify":
        return verify(audit_path(), state_path())
    if cmd == "bootstrap":
        return bootstrap(audit_path(), state_path())
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
