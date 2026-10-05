#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# Read-only: report current Compose state; never start, stop, or mutate containers.
compose_json="$(docker compose -f docker-compose.dev.yml ps --format json)"

python_bin="$(command -v python3)"
summary="$("$python_bin" - "$compose_json" <<'PY'
import importlib
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    module = importlib.import_module("clipos_api.infrastructure")
except Exception:
    module = None

if module is None:
    # Standard-library fallback keeps the command observable if the API package
    # is unavailable. It preserves the same output keys.
    services = []
    statuses = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        health = entry.get("Health") or {}
        state = health.get("Status")
        if state is None:
            state = "starting" if entry.get("State") == "running" else "unhealthy"
        if entry.get("State") is None and entry.get("Health") is None:
            state = "absent"
        statuses.append(state)
        services.append({
            "name": entry.get("Service", ""),
            "status": state,
            "detail": entry.get("Status"),
            "checked_at": entry.get("CreatedAt"),
        })
    overall = "healthy" if services and all(s == "healthy" for s in statuses) else "degraded"
    print(json.dumps({"services": services, "overall": overall}))
else:
    print(module.parse_compose_json(raw).model_dump_json())
PY
)"

printf '%s\n' "$summary"
"$python_bin" - "$summary" <<'PY'
import json
import sys
health = json.loads(sys.argv[1])
raise SystemExit(0 if health.get("overall") == "healthy" else 1)
PY
