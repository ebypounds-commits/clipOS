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
printf '%s\n' "$summary" | python3 -c '
import json, sys
health = json.loads(sys.stdin.read())
names = {s["name"]: s["status"] for s in health["services"]}
missing = [n for n in ("temporal", "temporal-ui") if names.get(n) != "healthy"]
raise SystemExit(1 if missing else 0)
'

# Temporal reachability probes (read-only, short timeout).
grpc_ok="$(docker compose -f docker-compose.dev.yml exec -T temporal \
  sh -c 'temporal operator cluster health --address $(hostname -i | cut -d" " -f1):7233' 2>/dev/null || true)"
if [ -z "$grpc_ok" ]; then
  echo "temporal gRPC (7233) unreachable inside compose network" >&2
  exit 1
fi

ns_ok=""
for i in 1 2 3 4 5 6 7 8 9 10; do
  if docker compose -f docker-compose.dev.yml exec -T temporal \
    temporal operator namespace describe --address temporal:7233 clipos-dev >/dev/null 2>&1; then
    ns_ok=1
    break
  fi
  sleep 2
done
if [ -z "$ns_ok" ]; then
  echo "namespace clipos-dev missing (temporal-init one-shot did not run?)" >&2
  exit 1
fi

if ! curl -fsS --max-time 3 http://localhost:8233/ >/dev/null 2>&1; then
  echo "temporal UI (host 8233) not responding" >&2
  exit 1
fi

echo "temporal healthy (gRPC 7233, UI 8233)"
