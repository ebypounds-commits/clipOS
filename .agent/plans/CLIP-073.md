# ExecPlan — CLIP-073 Revenue dashboards/reconciliation workflows

## Ticket and objective
CLIP-073 (M17): pure revenue dashboard roll-up + recorded-vs-expected
reconciliation runs with persisted audited records (PRD §I, journey 4).

## Current repository facts
- Head at plan time: `a072671` (CLIP-072 state advance, 78/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-073`, branch `agent/CLIP-073`,
  venv ready, lease active. Next free migration: **0051** (0050 -> 0051).

## Scope / non-scope
Scope: `clipos_api/revenue_reconciliation.py`, migration 0051, tests,
main.py wiring, test_migrations, docs/development.md.
Non-scope: automatic statement ingestion, multi-source consolidation UI,
alerting.

## Implementation
1. Pure reconcile_revenue (delta, delta_pct zero-expected rule,
   tolerance MATCH/DISCREPANCY) + summarize_revenue (per-currency
   totals, per-kind/source keyed `key:currency` so currencies never
   mix).
2. RevenueDashboardStore: dashboard with exact-window filters,
   run_reconciliation (source 404, period 422, idempotent/conflict,
   insert race re-check, audited recorded), list.
3. Migration 0051; 3 routes; docs.

## Test/gate plan
~15 tests; ruff/mypy; pytest SQLite + CI-env; PG 0050->0051;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (78->79, current -> CLIP-074)
-> push -> CI green -> release lease, remove worktree, local pull.