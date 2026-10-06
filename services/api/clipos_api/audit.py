"""Append-only structured audit logging for material state changes."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AUDIT_DIR = Path(".agent/audit")
AUDIT_PATH = AUDIT_DIR / "audit.log"


def record(action: str, actor: str, subject: str, details: dict[str, Any]) -> dict[str, Any]:
    if not action or not actor or not subject:
        raise ValueError("action, actor, and subject are required")
    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "action": action,
        "actor": actor,
        "subject": subject,
        "details": details,
    }
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as output:
        output.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    return event


def read_events(path: Path | None = None) -> list[dict[str, Any]]:
    """Read recorded audit events additively; the log itself is never rewritten.

    The path defaults to the CURRENT module-global AUDIT_PATH at call time
    (not import time) so tests can redirect the log via monkeypatching.
    """
    if path is None:
        path = AUDIT_PATH
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events
