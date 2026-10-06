#!/usr/bin/env python3
"""Validate BUILD_STATE.json against schemas/build_state.schema.json.

Stdlib-only pragmatic subset checker (no external jsonschema dependency).
Cross-checks the state against 10_TICKETS.yaml (ticket ids parsed textually)
and its own last_evidence record. Mutates nothing: state files are read-only.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "build_state.schema.json"
STATE_PATH = ROOT / "BUILD_STATE.json"
TICKETS_PATH = ROOT / "10_TICKETS.yaml"

TYPE_NAMES = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "boolean": bool,
}


def _check_type(value: Any, expected: str) -> bool:
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, TYPE_NAMES[expected])


def _validate(instance: Any, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if "type" in schema and not _check_type(instance, schema["type"]):
        return [f"{path}: expected type {schema['type']}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")
    if "pattern" in schema and not re.search(schema["pattern"], instance):
        errors.append(
            f"{path}: {instance!r} does not match pattern {schema['pattern']!r}"
        )
    if "oneOf" in schema and not any(
        not _validate(instance, sub, path) for sub in schema["oneOf"]
    ):
        errors.append(f"{path}: does not match any oneOf schema")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                errors.extend(_validate(instance[key], sub, f"{path}.{key}"))
    if isinstance(instance, list):
        if schema.get("uniqueItems") and len(instance) != len(
            {str(item) for item in instance}
        ):
            errors.append(f"{path}: items are not unique")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(instance):
                errors.extend(_validate(item, item_schema, f"{path}[{index}]"))
    return errors


def ticket_ids() -> list[str]:
    """Parse ticket ids from 10_TICKETS.yaml without PyYAML (stdlib only)."""
    ids: list[str] = []
    for line in TICKETS_PATH.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^-\s+id:\s*(\S+)", line)
        if match:
            ids.append(match.group(1))
    return ids


def validate_state(
    state: dict[str, Any], schema: dict[str, Any], tickets: list[str]
) -> list[str]:
    errors = _validate(state, schema, "BUILD_STATE")
    current = state.get("current_ticket")
    completed = state.get("completed_tickets", [])
    if (
        isinstance(current, str)
        and isinstance(completed, list)
        and current in completed
    ):
        errors.append(
            "BUILD_STATE.current_ticket must not also be in completed_tickets"
        )
    if isinstance(current, str) and tickets and current not in tickets:
        errors.append(
            f"BUILD_STATE.current_ticket {current!r} missing from 10_TICKETS.yaml"
        )
    evidence = state.get("last_evidence")
    if isinstance(evidence, dict) and isinstance(completed, list):
        evidence_ticket = evidence.get("ticket")
        if isinstance(evidence_ticket, str) and evidence_ticket not in completed:
            errors.append(
                f"BUILD_STATE.last_evidence.ticket {evidence_ticket!r} not in completed_tickets"
            )
    return errors


def main() -> int:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"state: invalid ({exc})", file=sys.stderr)
        return 1
    errors = validate_state(state, schema, ticket_ids())
    if errors:
        for error in errors:
            print(f"state: invalid ({error})", file=sys.stderr)
        return 1
    completed = state.get("completed_tickets", [])
    print(f"state: valid ({len(completed)} completed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
