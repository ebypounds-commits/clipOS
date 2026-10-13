# ExecPlan — CLIP-065 Analytics dashboards and scheduled collection

## Ticket and objective
CLIP-065 (M15): org-level analytics dashboard (pure roll-up over CLIP-064
performance snapshots) + operator-defined collection schedules with a
caller-invoked due-run executor (PRD §H + journey 3).

## Current repository facts
- Head at plan time: `bb59660` (CLIP-064 state advance, 70/102). CI green.
- CLIP-062 analytics_metrics (0040), CLIP-063 reconciliation (0041),
  CLIP-064 performance_snapshots (0042).
- Worktree `/srv/clipos/worktrees/CLIP-065`, branch `agent/CLIP-065`,
  venv ready, lease active.
- Next free migration: **0043** (0042 -> 0043).

## Scope / non-scope
Scope: `clipos_api/analytics_dashboards.py`, `clipos_api/collection_schedules.py`,
migration 0043, tests for both, main.py wiring, test_migrations,
docs/development.md.
Non-scope: background scheduler thread/Temporal workflow (caller-invoked
run-due only), frontend dashboard UI, new metric kinds.

## Implementation
1. analytics_dashboards: pure summarize_snapshots (totals, avg
   engagement, top-5 by engagement with deterministic tie-break),
   GET /analytics/dashboard?org_id=.
2. collection_schedules: table (unique (org, content_ref), unique
   schedule_id, ix org+enabled), register idempotent/conflict,
   run_due with injected clock (fail-soft: always advances next_run_at,
   adapter failure -> FAILED outcome), 3 routes, audits registered/executed.
3. Migration 0043; main.py; test_migrations += collection_schedules;
   docs.

## Test/gate plan
~20 tests across two files; ruff/mypy; pytest SQLite + CI-env; PG
0042->0043; OpenAPI/secret_scan/arch_guard/state_audit verify.

## Landing
Worktree commit -> merge --no-ff -> state advance (70->71, flip
CLIP-064... no: flip CLIP-065, current -> CLIP-066) -> push -> CI green
-> lease release, worktree removal.