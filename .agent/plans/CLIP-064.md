# ExecPlan — CLIP-064 Normalized performance snapshots

## Ticket and objective
CLIP-064 (M15): One normalized performance snapshot per content_ref from
stored analytics metrics (PRD §H). Pure normalization (engagement rate),
persistence table, routes.

## Current repository facts
- Head at plan time: `4397acf` (CLIP-063 state advance). CI green.
- CLIP-062: AnalyticsAdapter Protocol + analytics_metrics (0040).
- CLIP-063: reconciliation_records (0041).
- Worktree `/srv/clipos/worktrees/CLIP-064`, branch `agent/CLIP-064`.
- Next free migration: **0042** (0041 -> 0042).

## Scope / non-scope
Scope: `clipos_api/performance_snapshots.py`, migration 0042,
`tests/test_performance_snapshots.py`, main.py wiring, test_migrations,
docs/development.md.
Non-scope: experiments/cohorts, learned rules, retention curves beyond
stored watch-time, real platform SDKs.

## Files/domains expected to change
- New: `clipos_api/performance_snapshots.py`,
  `migrations/versions/0042_performance_snapshots.py`,
  `tests/test_performance_snapshots.py`, `.agent/plans/CLIP-064.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Security, rights, impact
No external side effects; mock metrics only. Audits:
performance_snapshots.recorded.

## Implementation steps
1. performance_snapshots.py: build_snapshot_fields pure (engagement_rate
   = (LIKE+COMMENT+SHARE)/VIEW*100), PerformanceSnapshotStore (idempotent
   (org,content), conflict), build_performance_snapshot flow, 3 routes.
2. Migration 0042; tests; main.py; test_migrations; docs.

## Test/gate plan
ruff format/check, mypy, pytest SQLite + CI-env, PG 0041->0042,
OpenAPI, secret_scan, arch_guard, state_audit verify.