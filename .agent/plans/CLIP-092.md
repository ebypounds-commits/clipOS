# ExecPlan — CLIP-092 Security/tenant/fuzz/dependency tests

## Ticket and objective
CLIP-092 (M22): make the security posture regression-tested in the
normal test run — tenant isolation, privilege-floor invariants, fail-closed
gates, hostile-input fuzzing, and dependency/supply-chain controls. This
is a test-only ticket: no new product module, no migration.

## Current repository facts
- Head at plan time: `bfcce1b` (CLIP-091 state advance, 97/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-092`, branch `agent/CLIP-092`,
  venv ready, lease active. No migration (test-only).
- Lane-11/10 controls already exist as scripts (`dependency_audit.sh`,
  `secret_scan.sh`); this ticket adds in-process regression coverage so a
  regression fails the ordinary pytest run.

## Scope / non-scope
Scope: three new test modules under `services/api/tests/`,
`.agent/plans/CLIP-092.md`, a development.md note.
Non-scope: new runtime modules, schema changes, CI topology changes,
qualification run (CLIP-093).

## Implementation
1. `test_security_tenant.py` — deny-by-default `require_membership`
   (unknown user/org, suspended org, removed member), the
   final-active-owner floor (demote/remove raise), and cross-tenant
   isolation across seven stores (analytics, budgets, experiments,
   threats, injection events, platform accounts).
2. `test_fuzz_security.py` — fixed 26-item hostile corpus plus a
   constant-seeded random generator; asserts the boundary never raises,
   never passes text on a QUARANTINE/SCANNER_ERROR, never allows hostile
   remediation values, and that no endpoint returns 500 on hostile input.
   Includes a re-scan of neutralized output (no CRITICAL survives).
3. `test_dependency_security.py` — dependencies bounded (no bare pins),
   no non-PyPI index, no repo pip config, CI still runs
   dependency-audit/secret-scan/SBOM plus format/lint/type/tests, the
   Dockerfile switches to a non-root USER, docker.sock is never mounted,
   and an in-process scan finds no private key or provider-looking secret
   in any tracked file.

## Test/gate plan
Three modules, deterministic (no wall clock, no unseeded randomness).
Gates: ruff/mypy, full pytest SQLite + CI-env, OpenAPI probe (unchanged
path count), secret_scan, architecture_guard, state_audit verify. No PG
migration step.

## Landing
Commit -> merge --no-ff -> state advance (97->98, current -> CLIP-093)
-> push -> CI green -> release lease, remove worktree, local pull.