#!/usr/bin/env bash
# Run due collection schedules.
#
# This script is intended to be run periodically (e.g., every minute via cron)
# to execute analytics collection schedules that are due.
#
# Usage:
#   bash scripts/run_collection_schedules.sh
#
# Environment variables (defaults suit the development stack):
#   DATABASE_URL              database connection string
#   CACHE_REDIS_URL           redis URL
#   STORAGE_ENDPOINT          MinIO/S3 endpoint
#   STORAGE_ACCESS_KEY        MinIO access key
#   STORAGE_SECRET_KEY        MinIO secret key
#   STORAGE_BUCKET            bucket name
#   TEMPORAL_ADDRESS          Temporal address
#   COLLECTION_SCHEDULES_API  API base URL (default: http://localhost:8000)

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

url="${COLLECTION_SCHEDULES_API:-http://localhost:8000}"

# If API URL points to localhost, run via local Python (faster, no network)
if [[ "$url" == http://localhost:* ]] || [[ "$url" == http://127.0.0.1:* ]]; then
  echo "run_collection_schedules: running via local Python module"
  PYTHONPATH=services/api \
  DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://clipos_dev:clipos_dev_local_only@localhost:5433/clipos_dev}" \
  STORAGE_ENDPOINT="${STORAGE_ENDPOINT:-http://localhost:9002}" \
  STORAGE_ACCESS_KEY="${STORAGE_ACCESS_KEY:-clipos_dev}" \
  STORAGE_SECRET_KEY="${STORAGE_SECRET_KEY:-clipos_dev_local_only}" \
  STORAGE_BUCKET="${STORAGE_BUCKET:-clipos-test}" \
  CACHE_REDIS_URL="${CACHE_REDIS_URL:-redis://localhost:6380/0}" \
  TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}" \
  /srv/clipos/e2e-venv/bin/python -m clipos_api.collection_schedules --org-id all 2>&1
else
  echo "run_collection_schedules: calling remote API at $url"
  # Get all orgs that have enabled schedules (requires admin or service token)
  # For now, we assume the caller knows the org IDs
  org_ids="${COLLECTION_SCHEDULES_ORGS:-}"
  if [[ -z "$org_ids" ]]; then
    echo "run_collection_schedules: COLLECTION_SCHEDULES_ORGS not set; skipping remote call" >&2
    exit 1
  fi
  for org_id in ${org_ids//,/ }; do
    echo "run_collection_schedules: running due schedules for org $org_id"
    curl -fsS -X POST "$url/collection-schedules/run-due" \
      -H "Content-Type: application/json" \
      -d "{\"org_id\": \"$org_id\"}" | jq .
  done
fi