# ExecPlan — CLIP-075 Budgets/spend authorization

## Ticket and objective
CLIP-075 (M18): fail-closed budget authorization before provider
spend — pure budget math, ledger-backed spent totals, persisted
decisions (PRD §I budgets).

## Current repository facts
- Head at plan time: `0d1c01d` (CLIP-074 state advance, 80/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-075`, branch `agent/CLIP-075`,
  venv ready, lease active. Next free migration: **0053** (0052 -> 0053).
- CLIP-074 cost_ledger provides cost_entries + cost_sources (spend
  basis: exact window+currency, source cost_center == scope_key).

## Scope / non-scope
Scope: `clipos_api/budgets.py`, migration 0053 (budgets +
spend_authorizations), tests, main.py wiring, test_migrations,
docs/development.md.
Non-scope: real-time spend gating middleware, budget alerts,
multi-currency auto-conversion.

## Implementation
1. Pure authorize_spend (boundary allowed, remaining_after None on
   deny); currency mismatch hard deny in store.
2. BudgetStore: register idempotent/conflict/period-422; authorize
   (budget 404, ledger-sum spent, persist every outcome, audited,
   idempotent conflict); list.
3. Migration 0053; 5 routes; docs.

## Test/gate plan
~16 tests; ruff/mypy; pytest SQLite + CI-env; PG 0052->0053;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (80->81, current -> CLIP-076)
-> push -> CI green -> release lease, remove worktree, local pull.