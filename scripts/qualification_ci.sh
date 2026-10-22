#!/usr/bin/env bash
# Run the production qualification harnesses against a live ClipOS API.
#
# The harnesses are the controls that protect the hard requirements: the
# rights gate, the private-mode policy, the secret boundary and the audit
# trail. They are only meaningful against a running service, so this script
# starts one on the development network, runs both harnesses, and fails when
# either reports a failure.
#
# The API container is started on the compose network so it reaches the
# development Postgres, Redis, MinIO and Temporal by service name. The
# harnesses themselves speak plain HTTP.
#
# Usage:
#   bash scripts/qualification_ci.sh
#
# Environment overrides (defaults suit the development stack):
#   QUALIFICATION_PORT          host port for the API      (default 18098)
#   QUALIFICATION_NETWORK       docker network to join     (default clipos-dev_default)
#   QUALIFICATION_DATABASE_URL  database inside the network
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

compose="docker compose -f docker-compose.dev.yml --profile app"
image="clipos-dev-api"
container="clipos-qualification-api"
port="${QUALIFICATION_PORT:-18098}"
url="http://localhost:${port}"
network="${QUALIFICATION_NETWORK:-clipos-dev_default}"
database_url="${QUALIFICATION_DATABASE_URL:-postgresql+psycopg://clipos_dev:clipos_dev_local_only@postgres:5432/clipos_dev}"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "qualification_ci: building the API image"
$compose build api

echo "qualification_ci: starting the API on ${url}"
docker run -d --name "$container" \
  --network "$network" \
  -p "${port}:8000" \
  -e ENVIRONMENT=development \
  -e DATABASE_URL="$database_url" \
  -e CACHE_REDIS_URL=redis://redis:6379/0 \
  -e STORAGE_ENDPOINT=http://minio:9000 \
  -e STORAGE_ACCESS_KEY=clipos_dev \
  -e STORAGE_SECRET_KEY=clipos_dev_local_only \
  -e TEMPORAL_ADDRESS=temporal:7233 \
  "$image" >/dev/null

body=""
for _ in $(seq 1 45); do
  if body="$(curl -fsS "${url}/health" 2>/dev/null)"; then
    break
  fi
  sleep 2
done

if [ -z "$body" ]; then
  echo "qualification_ci: the API did not become healthy on ${url}" >&2
  docker logs "$container" >&2 || true
  exit 1
fi
echo "qualification_ci: API healthy ${body}"

echo "qualification_ci: security qualification"
python3 scripts/security_qualification.py --base-url "$url" --strict

echo "qualification_ci: private-mode qualification"
python3 scripts/private_mode_qualification.py --base-url "$url" --strict

echo "qualification_ci: ok"
