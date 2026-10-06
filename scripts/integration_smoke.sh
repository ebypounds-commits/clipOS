#!/usr/bin/env bash
# Read-only integration smoke against the running clipos-dev stack.
# Executes real PostgreSQL, Redis, and MinIO readiness checks inside the
# existing containers. Idempotent: never starts, stops, or mutates anything.
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

dc="docker compose -f docker-compose.dev.yml"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

failed=0
checks=()

record() {
  checks+=("$1")
}

run_check() {
  local name="$1"
  shift
  if "$@" >"$tmp" 2>&1; then
    record "{\"check\": \"$name\", \"status\": \"ok\"}"
  else
    record "{\"check\": \"$name\", \"status\": \"failed\"}"
    printf 'integration_smoke: %s failed:\n' "$name" >&2
    sed 's/^/  /' "$tmp" >&2
    failed=1
  fi
}

run_check postgres_psql_select_1 \
  $dc exec -T postgres psql -U clipos_dev -d clipos_dev -tAc "SELECT 1;"

run_check redis_ping \
  $dc exec -T redis redis-cli PING

run_check minio_ready \
  $dc exec -T minio mc ready local

printf '{\n  \"checks\": [\n'
printf '    %s,\n' "${checks[0]}"
printf '    %s,\n' "${checks[1]}"
printf '    %s\n' "${checks[2]}"
printf '  ],\n  \"status\": \"%s\"\n}\n' "$([ "$failed" -eq 0 ] && echo ok || echo failed)"

exit "$failed"
