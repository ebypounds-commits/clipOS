# ExecPlan — CLIP-076 Profitability/reinvestment calculations

## Ticket and objective
CLIP-076 (M18): pure profit/margin math + integer reinvest/reserve
split over the revenue/cost ledgers, persisted as reports (PRD §I
profitability, reserve/reinvestment policies).

## Current repository facts
- Head at plan time: `51ff576` (CLIP-075 state advance + ruff fix,
  81/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-076`, branch `agent/CLIP-076`,
  venv ready, lease active. Next free migration: **0054** (0053 -> 0054).

## Scope / non-scope
Scope: `clipos_api/profitability.py`, migration 0054, tests,
main.py wiring, test_migrations, docs/development.md.
Non-scope: auto-reinvestment execution, tax handling, multi-currency
conversion.

## Implementation
1. Pure calculate_profitability (margin rules for zero revenue) +
   calculate_reinvestment (integer floor split, non-positive profit
   zeros, 0..100 validation).
2. ProfitabilityStore: run_report sums both ledgers exact
   window+currency, computes, persists (idempotent/conflict/period-422,
   insert race re-check, audited), list.
3. Migration 0054; 2 routes; docs.

## Test/gate plan
~14 tests; ruff/mypy; pytest SQLite + CI-env; PG 0053->0054;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (81->82, current -> CLIP-077)
-> push -> CI green -> release lease, remove worktree, local pull.