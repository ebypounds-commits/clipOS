# ExecPlan — CLIP-074 Cost ledger and provider cost ingestion

## Ticket and objective
CLIP-074 (M18 opener): provider-neutral cost ingestion — CostAdapter
Protocol + mock, registered cost sources, append-only integer-cents
cost entries with idempotent period sync (PRD §I cost ledger).

## Current repository facts
- Head at plan time: `e29547d` (CLIP-073 state advance, 79/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-074`, branch `agent/CLIP-074`,
  venv ready, lease active. Next free migration: **0052** (0051 -> 0052).
- `llm_cost.CostLedger` already records in-process model dispatch
  spend — this module records provider bills (distinct tables).

## Scope / non-scope
Scope: `clipos_api/cost_ledger.py`, migration 0052 (cost_sources +
cost_entries), tests, main.py wiring, test_migrations,
docs/development.md.
Non-scope: profitability/reserve policies, budget enforcement, frontend.

## Implementation
1. CostAdapter Protocol + MockCostAdapter (deterministic USAGE+STORAGE
   entries, sha1-derived refs/amounts).
2. CostLedgerStore: register idempotent/conflict, sync (source 404,
   period 422, skip existing, audited per insert), list.
3. Migration 0052; 5 routes; docs.

## Test/gate plan
~14 tests; ruff/mypy; pytest SQLite + CI-env; PG 0051->0052;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (79->80, current -> CLIP-075)
-> push -> CI green -> release lease, remove worktree, local pull.