# ExecPlan — CLIP-081 Brand lifecycle and shutdown/archive policies

## Ticket and objective
CLIP-081 (M19): guarded brand lifecycle (DRAFT->ACTIVE->SUSPENDED->
RETIRED with resume) + declared shutdown policies per brand (PRD §K).

## Current repository facts
- Head at plan time: `0935629` (CLIP-080 state advance, 86/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-081`, branch `agent/CLIP-081`,
  venv ready, lease active. Next free migration: **0059** (0058 -> 0059).

## Scope / non-scope
Scope: `clipos_api/brand_lifecycle.py`, migration 0059 (brand_lifecycles
+ shutdown_policies), tests, main.py wiring, test_migrations,
docs/development.md.
Non-scope: shutdown enforcement workflow, content deletion jobs,
frontend.

## Implementation
1. Pure lifecycle_transition_allowed (resume allowed, RETIRED
   terminal, same-state noop).
2. BrandLifecycleStore: lifecycle one-per-brand (unique natural key),
   brand 404, transitions audited; shutdown one-per-brand, brand 404,
   grace 0..90, idempotent/conflict, audited.
3. Migration 0059; 5 routes; docs.

## Test/gate plan
~14 tests; ruff/mypy; pytest SQLite + CI-env; PG 0058->0059;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (86->87, current -> CLIP-082)
-> push -> CI green -> release lease, remove worktree, local pull.