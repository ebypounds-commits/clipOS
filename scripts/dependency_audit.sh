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

# Scope: audit OUR declared dependencies (requirements-audit.txt), never the
# host environment's full site-packages (on CI runners --local would audit
# unrelated preinstalled tooling and produce false positives).
if "$PIPAUDIT" -r requirements-audit.txt --help >/dev/null 2>&1 || [ -f requirements-audit.txt ]; then
  if "$PIPAUDIT" -r requirements-audit.txt "${args[@]}"; then
    exit 0
  else
    rc=$?
    # -r with a local package path is unsupported on old pip-audit: fall back
    # to the installed environment, which for CI/local venvs IS our dependency set.
    if [ "$rc" -eq 2 ] || [ "$rc" -eq 3 ]; then
      echo "dependency_audit: requirement-mode unsupported (rc=$rc), falling back to --local" >&2
      exec "$PIPAUDIT" --local "${args[@]}"
    fi
    exit "$rc"
  fi
fi
exec "$PIPAUDIT" --local "${args[@]}"
