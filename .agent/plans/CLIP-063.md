# ExecPlan — CLIP-063 Native metric ingestion/reconciliation

## Ticket and objective
CLIP-063 (M15): Metric ingestion and reconciliation building on CLIP-062
analytics contract. Collects metrics via adapter, compares against
previous snapshot, records discrepancies. Append-only audit trail.

## Current repository facts
- Head at plan time: `595868b` (CLIP-062 ruff fix). CI green.
- CLIP-062: AnalyticsAdapter Protocol, MockAnalyticsAdapter, analytics_metrics
  (migration 0040), collect flow, 2 routes.
- Worktree `/srv/clipos/worktrees/CLIP-063`, branch `agent/CLIP-063`,
  venv ready.
- Next free migration: **0041** (0040 -> 0041).

## Scope / non-scope
Scope: `clipos_api/metric_reconciliation.py`, migration 0041,
`tests/test_metric_reconciliation.py`, main.py wiring, test_migrations,
docs/development.md.
Non-scope: real platform analytics SDKs, experiments/cohorts, learned
rules, advanced anomaly detection.

## Files/domains expected to change
- New: `clipos_api/metric_reconciliation.py`,
  `migrations/versions/0041_reconciliation.py`,
  `tests/test_metric_reconciliation.py`, `.agent/plans/CLIP-063.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Implementation steps
1. metric_reconciliation.py: reconcile_metrics flow (collect via adapter,
   compare against stored metrics, record discrepancies),
   ReconciliationStore, 2 routes.
2. Migration 0041: reconciliation_records table.
3. Tests, main.py wiring, test_migrations, docs.

## Test/gate plan
- ruff format/check, mypy, full pytest SQLite, PG alembic 0040->0041,
  full pytest CI-env, OpenAPI probe, secret_scan, architecture_guard,
  state_audit verify.