# ExecPlan — CLIP-079 Channel incubation/launch experiments

## Ticket and objective
CLIP-079 (M19): guarded incubation lifecycle for new channels —
DRAFT -> INCUBATING -> LAUNCHED/SUNSET -> ARCHIVED with template
linkage (PRD journey rollout).

## Current repository facts
- Head at plan time: `0df29ff` (CLIP-078 state advance, 84/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-079`, branch `agent/CLIP-079`,
  venv ready, lease active. Next free migration: **0057** (0056 -> 0057).

## Scope / non-scope
Scope: `clipos_api/incubation.py`, migration 0057, tests, main.py
wiring, test_migrations, docs/development.md.
Non-scope: automated goal measurement, launch checklists UI, content
scheduling.

## Implementation
1. Pure run_transition_allowed (launch must pass INCUBATING;
   same-state noop; ARCHIVED terminal).
2. IncubationStore: register idempotent/conflict (brand/channel
   template links 404 same-org), transition audited actor/reason,
   list status filter.
3. Migration 0057; 4 routes; docs.

## Test/gate plan
~14 tests; ruff/mypy; pytest SQLite + CI-env; PG 0056->0057;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (84->85, current -> CLIP-080)
-> push -> CI green -> release lease, remove worktree, local pull.