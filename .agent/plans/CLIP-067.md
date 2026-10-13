# ExecPlan — CLIP-067 Deterministic assignment/variant orchestration

## Ticket and objective
CLIP-067 (M16): deterministic subject->variant assignment over the
CLIP-066 experiment records — weighted variants, sha256 bucketing,
persisted idempotent assignments, summary counts.

## Current repository facts
- Head at plan time: `c3a221f` (CLIP-066 state advance, 72/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-067`, branch `agent/CLIP-067`,
  lease active. Next free migration: **0045** (0044 -> 0045).

## Scope / non-scope
Scope: `clipos_api/experiment_orchestration.py`, migration 0045
(experiment_variants + experiment_assignments), tests, main.py wiring,
test_migrations, docs/development.md.
Non-scope: cohort analysis, results evaluation, traffic splitting
infra, frontend.

## Implementation
1. Pure: deterministic_bucket (sha256 mod total weight), select_variant
   (sorted cumulative-weight selection).
2. OrchestrationStore: register_variant (DRAFT-only, idempotent,
   conflict 409), assign (ACTIVE-only, >=2 variants, persisted idempotent
   — stored assignment never recomputed), list, variant_summary counts.
3. Migration 0045; 5 routes; audits registered/assigned; docs.

## Test/gate plan
~18 tests; ruff/mypy; pytest SQLite + CI-env; PG 0044->0045;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (72->73, current -> CLIP-068)
-> push -> CI green -> release lease, remove worktree, local pull.