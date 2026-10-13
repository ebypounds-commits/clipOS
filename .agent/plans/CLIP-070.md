# ExecPlan — CLIP-070 Revenue-source/ledger adapters

## Ticket and objective
CLIP-070 (M17 opener): provider-neutral revenue ingestion — adapter
Protocol + mock, registered sources, append-only integer-cents ledger
with idempotent period sync (PRD §I monetization).

## Current repository facts
- Head at plan time: `65ffa58` (CLIP-069 state advance, 75/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-070`, branch `agent/CLIP-070`,
  venv verified, lease active. Next free migration: **0048** (0047 -> 0048).

## Scope / non-scope
Scope: `clipos_api/revenue_ledger.py`, migration 0048 (revenue_sources +
revenue_entries), tests, main.py wiring, test_migrations,
docs/development.md.
Non-scope: real platform revenue APIs, cost ledger (llm_cost exists),
profitability/reserve policies, frontend.

## Implementation
1. RevenueAdapter Protocol + MockRevenueAdapter (deterministic 2
   entries: PLATFORM+AFFILIATE, sha1-derived refs/amounts).
2. RevenueLedgerStore: register_source idempotent/conflict, sync
   (source 404, period validation, per-entry idempotent skip,
   audited recorded), list sources/entries.
3. Migration 0048 (integer amount_cents BigInteger, unique
   (org, source, external_ref)); 5 routes; docs.

## Test/gate plan
~16 tests; ruff/mypy; pytest SQLite + CI-env; PG 0047->0048;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (75->76, current -> CLIP-071)
-> push -> CI green -> release lease, remove worktree, local pull.