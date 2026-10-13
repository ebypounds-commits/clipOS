# ExecPlan — CLIP-069 Strategy-rule canary/promotion/rollback

## Ticket and objective
CLIP-069 (M16): learned strategy rules with a guarded rollout
lifecycle — canary, promote, rollback, re-canary (PRD journey 5).

## Current repository facts
- Head at plan time: `58cbc45` (CLIP-068 state advance, 74/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-069`, branch `agent/CLIP-069`,
  lease active. Next free migration: **0047** (0046 -> 0047).

## Scope / non-scope
Scope: `clipos_api/strategy_rules.py`, migration 0047, tests,
main.py wiring, test_migrations, docs/development.md.
Non-scope: rule execution/enforcement engine, automatic promotion
based on evaluation results, frontend.

## Implementation
1. Pure rule_transition_allowed: DRAFT->CANARY, CANARY->ACTIVE or
   ROLLED_BACK, ACTIVE->ROLLED_BACK, ROLLED_BACK->CANARY; never skip
   canary; same-state noop.
2. StrategyRuleStore: register idempotent/conflict, transition audited
   (actor/reason refs), list with status filter.
3. Migration 0047; 4 routes; docs.

## Test/gate plan
~15 tests; ruff/mypy; pytest SQLite + CI-env; PG 0046->0047;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (74->75, current -> CLIP-070)
-> push -> CI green -> release lease, remove worktree, local pull.