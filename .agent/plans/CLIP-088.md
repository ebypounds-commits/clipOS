# ExecPlan — CLIP-088 Safe auto-remediation actions

## Ticket and objective
CLIP-088 (M21): bounded remediation so incidents can be answered
without unrestricted command execution — allow-listed actions,
parameter-injection-proof pure gate, kill switch, attempt caps,
mandatory approval flag, recorded runs (PRD §K).

## Current repository facts
- Head at plan time: `4c5fa7e` (CLIP-087 state advance, 93/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-088`, branch `agent/CLIP-088`,
  venv ready, lease active. Next free migration: **0065** (0064 -> 0065).

## Scope / non-scope
Non-scope: real infrastructure executors, approval workflow UI,
alerting.

## Implementation
1. Pure template validation (placeholders, shell-metacharacter and
   bad-placeholder rejection) + evaluate_remediation gate with
   explicit denial reasons in fixed order (kill switch -> disabled ->
   params -> approval -> rate cap).
2. Store: register (template validated pre-persist, idempotent/
   conflict, audited), run_remediation (gate, execute only when
   allowed, executor failures recorded, denied never execute,
   idempotent/conflict, insert race re-check, audited), list.
3. Migration 0065; 4 routes; docs.

## Test/gate plan
~18 tests incl. injection-attempt table; ruff/mypy; pytest SQLite +
CI-env; PG 0064->0065; OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (93->94, current -> CLIP-089)
-> push -> CI green -> release lease, remove worktree, local pull.