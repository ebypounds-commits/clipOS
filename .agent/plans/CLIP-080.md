# ExecPlan — CLIP-080 Portfolio allocation/quota engine

## Ticket and objective
CLIP-080 (M19): per-account/platform publication quota gate (rolling
windows over publication_jobs) + deterministic largest-remainder
portfolio allocator with persisted results (PRD §K caps, §H
allocation).

## Current repository facts
- Head at plan time: `ea004fb` (CLIP-079 state advance, 85/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-080`, branch `agent/CLIP-080`,
  venv ready, lease active. Next free migration: **0058** (0057 -> 0058).

## Scope / non-scope
Scope: `clipos_api/portfolio_quotas.py`, migration 0058 (quota_policies
+ portfolio_allocations), tests, main.py wiring, test_migrations,
docs/development.md.
Non-scope: scheduler enforcement hooks, per-campaign budgets (CLIP-075
owns those), frontend.

## Implementation
1. Pure evaluate_quota (boundary allowed) + allocate_portfolio
   (largest remainder, deterministic key-order tie-break, always sums
   to total).
2. QuotaStore: register idempotent/conflict/audited; check_quota
   counts publication_jobs in rolling window scoped ACCOUNT/PLATFORM
   (audited quota_checks.evaluated); record_allocation persist +
   idempotent/conflict + audited; list.
3. Migration 0058; 5 routes; docs.

## Test/gate plan
~16 tests; ruff/mypy; pytest SQLite + CI-env; PG 0057->0058;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (85->86, current -> CLIP-081)
-> push -> CI green -> release lease, remove worktree, local pull.