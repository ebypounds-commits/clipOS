#!/usr/bin/env bash
# Python dependency vulnerability gate. Fails on findings except CVEs listed
# in scripts/audit-ignores.txt ("<CVE> # justification", one per line).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

PIPAUDIT="$(command -v pip-audit 2>/dev/null || true)"
if [ -z "$PIPAUDIT" ] && [ -x .venv/bin/pip-audit ]; then
  PIPAUDIT=.venv/bin/pip-audit
fi
if [ -z "$PIPAUDIT" ]; then
  echo "dependency_audit: pip-audit not found (install with: pip install -e 'services/api[dev]')" >&2
  exit 127
fi

args=()
while IFS= read -r cve; do
  [ -n "$cve" ] && args+=(--ignore-vuln "$cve")
done < <(sed -n 's/^[[:space:]]*\([A-Za-z0-9][A-Za-z0-9._-]*\)[[:space:]]*#[[:space:]]*[^[:space:]].*$/\1/p' scripts/audit-ignores.txt)

# Prefer auditing the installed environment (works in CI system env and local
# venvs alike); fall back to the pinned requirements file if --local is absent.
if "$PIPAUDIT" --help 2>&1 | grep -q -- '--local'; then
  exec "$PIPAUDIT" --local "${args[@]}"
fi
exec "$PIPAUDIT" -r requirements-audit.txt "${args[@]}"
