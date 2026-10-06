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
