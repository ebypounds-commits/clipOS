# ExecPlan — CLIP-068 Result evaluation/business significance

## Ticket and objective
CLIP-068 (M16): pure two-sample significance evaluation over variant
samples + persisted evaluation records with business-significance gate
(Statistical vs business significance split per PRD journey 4).

## Current repository facts
- Head at plan time: `29b0d07` (CLIP-067 state advance, 73/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-068`, branch `agent/CLIP-068`,
  lease active. Next free migration: **0046** (0045 -> 0046).

## Scope / non-scope
Scope: `clipos_api/experiment_evaluations.py`, migration 0046, tests,
main.py wiring, test_migrations, docs/development.md.
Non-scope: metric ingestion from analytics into auto-summaries
(caller-supplied stats for now), multi-arm correction, frontend.

## Implementation
1. Pure evaluate_significance: Welch t-test normal approximation
   (erfc-based two-sided p), |z| >= z_critical, |lift| >= min_effect,
   winner only when both gates pass; zero-SE handling.
2. EvaluationStore: requires experiment (404), idempotent (org, eval_id)
   conflict 409, audited recorded.
3. Routes: POST /experiments/{id}/evaluate, GET
   /experiments/{id}/evaluations. Migration 0046; docs.

## Test/gate plan
~16 tests; ruff/mypy; pytest SQLite + CI-env; PG 0045->0046;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (73->74, current -> CLIP-069)
-> push -> CI green -> release lease, remove worktree, local pull.