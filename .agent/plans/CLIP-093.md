# ExecPlan — CLIP-093 Production security qualification

## Ticket and objective
CLIP-093 (M22 closer): run the production security qualification and
remediate findings. Delivered as a runnable harness
(`scripts/security_qualification.py`) that re-checks the
07_SECURITY_GOVERNANCE_RIGHTS.md controls against a LIVE service and
emits a machine-checkable report, plus tests proving the harness itself
cannot pass a broken target.

## Current repository facts
- Head at plan time: `e8011e7` (CLIP-092 state advance, 98/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-093`, branch `agent/CLIP-093`,
  venv ready, lease active. No migration (qualification tooling + tests).
- M22 evidence now exists to qualify: rights gate (CLIP-033), publishing
  policy/kill switch (CLIP-058), injection guard + tool authorization
  (CLIP-091), remediation kill switch (CLIP-088), audit trail, and the
  CLIP-092 regression suites.

## Scope / non-scope
Scope: `scripts/security_qualification.py`,
`services/api/tests/test_security_qualification.py`,
`.agent/plans/CLIP-093.md`, development.md note.
Non-scope: new API modules, schema changes, penetration testing against
third parties, production deployment (M23).

## Implementation
1. Eleven controls C1-C11 checked over HTTP against a live instance:
   reachability, operator health + no secret leakage, OBSERVE-mode
   denial (kill switch), PUBLIC refused by default, rights gate denial,
   critical injection quarantined with empty safe text, benign content
   still ALLOWED (false-positive guard), untrusted content cannot
   authorize a tool, remediation kill switch denies hostile input and
   returns no rendered command, audit trail grows on a privileged
   action, and a final secret-pattern scan over every observed response.
2. Report shape: tool/version/generated_at/base_url/org_id,
   summary{total,passed,warnings,failed,verdict}, checks[], and
   findings[] (FAIL -> HIGH, WARN -> LOW, with recommendations).
3. Exit codes: 0 pass, 1 any FAIL (or WARN under --strict), 2 target
   unreachable so a broken environment is never mistaken for a pass.

## Test/gate plan
Ten tests drive the harness through a TestClient-backed transport:
all-PASS against the live app, schema stability, C1-C11 coverage, JSON
serializability, findings/non-pass parity, two deliberate-regression
tests proving a broken quarantine or tool-authorization rule turns the
check FAIL and the verdict FAIL, unreachable target -> exit 2, CLI JSON
output, and --strict warning semantics. Gates: ruff/mypy, full pytest
SQLite + CI-env, OpenAPI unchanged, secret_scan, architecture_guard,
state_audit verify. No PG migration step.

## Landing
Commit -> merge --no-ff -> state advance (98->99, M22 COMPLETE,
current_milestone M23, current -> CLIP-094) -> push -> CI green ->
release lease, remove worktree, local pull.