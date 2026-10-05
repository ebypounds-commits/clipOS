#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# Read-only: report current Compose state; never start, stop, or mutate containers.
compose_json="$(docker compose -f docker-compose.dev.yml ps --format json || true)"

summary="$(PYTHONPATH="$repo_root/services/api${PYTHONPATH:+:$PYTHONPATH}" python3 - "$compose_json" <<'PY'
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""

try:
    from clipos_api.infrastructure import parse_compose_json
    print(parse_compose_json(raw).model_dump_json())
except Exception:
    # stdlib fallback with identical semantics (Health as str OR dict)
    services = []
    statuses = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        health = entry.get("Health")
        if isinstance(health, dict):
            state = health.get("Status")
        else:
            state = health
        if state not in ("healthy", "unhealthy", "starting"):
            engine_state = entry.get("State")
            if engine_state == "running":
                state = "starting"
            elif not engine_state:
                state = "absent"
            else:
                state = "unhealthy"
        statuses.append(state)
        services.append({
            "name": entry.get("Service") or entry.get("Name") or "unknown",
            "status": state,
            "detail": entry.get("Status"),
            "checked_at": entry.get("CreatedAt"),
        })
    overall = "healthy" if services and all(s == "healthy" for s in statuses) else "degraded"
    print(json.dumps({"services": services, "overall": overall}))
PY
)"

printf '%s\n' "$summary"
python3 - "$summary" <<'PY'
import json
import sys
health = json.loads(sys.argv[1])
raise SystemExit(0 if health.get("overall") == "healthy" else 1)
PY
