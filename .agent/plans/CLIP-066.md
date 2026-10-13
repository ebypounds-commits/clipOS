# ExecPlan — CLIP-066 Experiment/hypothesis data model

## Ticket and objective
CLIP-066 (M16 opener): typed experiment records with audited status
lifecycles and linked hypotheses (PRD §H experiments/cohets).

## Current repository facts
- Head at plan time: `fb8e592` (CLIP-065 state advance, 71/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-066`, branch `agent/CLIP-066`,
  venv ready, lease active. Next free migration: **0044** (0043 -> 0044).

## Scope / non-scope
Scope: `clipos_api/experiments.py`, migration 0044 (experiments +
hypotheses tables), `tests/test_experiments.py`, main.py wiring,
test_migrations, docs/development.md.
Non-scope: cohort assignment, variant payloads, metric evaluation
against results (later M16 tickets), frontend.

## Implementation
1. Pure transition tables: experiment DRAFT->ACTIVE->COMPLETED->
   ARCHIVED (+DRAFT->ARCHIVED), hypothesis PROPOSED->SUPPORTED/REJECTED
   then frozen; same-state no-op allowed; illegal -> 409.
2. ExperimentStore: register idempotent (org, experiment_id) slug-
   validated natural id, conflict 409; hypothesis register requires
   linked experiment in same org (404); audits registered +
   status_changed (actor/reason refs).
3. Routes: POST/GET /experiments (+status filter), POST
   /experiments/{id}/transition, POST/GET /hypotheses (+experiment
   filter), POST /hypotheses/{id}/transition.
4. Migration 0044; docs.

## Test/gate plan
~20 tests (pure tables, store, routes, 404/409/422 slug); ruff/mypy;
pytest SQLite + CI-env; PG 0043->0044; OpenAPI/secret_scan/arch_guard/
state_audit.

## Landing
Commit -> merge --no-ff -> state advance (71->72, current -> CLIP-067)
-> push -> CI green -> release lease, remove worktree.