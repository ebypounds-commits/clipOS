#!/usr/bin/env bash
# E2E smoke: build the API image, run it on the reserved ad-hoc port 18099,
# poll /health, assert the JSON status field, and always clean up.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

compose="docker compose -f docker-compose.dev.yml --profile app"
image="clipos-dev-api"
container="clipos-e2e-api"
url="http://localhost:18099/health"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

$compose build api

docker run -d --name "$container" -p 18099:8000 "$image" >/dev/null

body=""
for _ in $(seq 1 30); do
  if body="$(curl -fsS "$url" 2>/dev/null)"; then
    break
  fi
  sleep 1
done

if [ -z "$body" ]; then
  echo "e2e_smoke: API did not become healthy on $url within 30s" >&2
  docker logs "$container" >&2 || true
  exit 1
fi

PYTHONPATH="$repo_root/services/api" python3 - "$body" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
assert "status" in payload, f"missing status field: {payload!r}"
PY

printf 'e2e_smoke: ok %s\n' "$body"
