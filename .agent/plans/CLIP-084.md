# ExecPlan — CLIP-084 Eval sandbox and safe promotion

## Ticket and objective
CLIP-084 (M20): isolated eval sandboxes for proposals + fail-closed
promotion gate that only allows PASSED sandbox + ready proposal
(PRD journey 5 tail).

## Current repository facts
- Head at plan time: `4c78277` (CLIP-083 state advance, 89/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-084`, branch `agent/CLIP-084`,
  venv ready, lease active. Next free migration: **0061** (0060 -> 0061).

## Scope / non-scope
Scope: `clipos_api/eval_sandbox.py`, migration 0061 (eval_sandboxes +
promotions), tests, main.py wiring, test_migrations, docs.
Non-scope: actual eval execution harness, model inference, frontend.

## Implementation
1. Pure sandbox_transition_allowed (CREATED->RUNNING->PASSED/FAILED,
   FAILED re-run, same-state noop) + evaluate_promotion gate matrix.
2. EvalStore: sandbox register (proposal 404, idempotent/conflict,
   audited), transitions audited; promotion evaluate+persist
   (proposal/sandbox 404, idempotent/conflict, audited evaluated).
3. Migration 0061; 5 routes; docs.

## Test/gate plan
~15 tests; ruff/mypy; pytest SQLite + CI-env; PG 0060->0061;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (89->90, current -> CLIP-085)
-> push -> CI green -> release lease, remove worktree, local pull.