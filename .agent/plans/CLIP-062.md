# ExecPlan — CLIP-062 Analytics provider contract

## Ticket and objective
CLIP-062 (M15): Analytics adapter implementing a provider-neutral
Protocol for metric collection from published content (PRD §H Analytics
& Learning). Mock adapter for CI; real platform adapters register later.

## Current repository facts
- Head at plan time: `7add751` (CLIP-061 state advance, M14 CLOSED). CI green.
- M14 complete: publishing adapter contract (CLIP-058), YouTube/TikTok/Meta
  adapters (CLIP-059/060/061).
- Worktree `/srv/clipos/worktrees/CLIP-062`, branch `agent/CLIP-062`,
  venv ready.
- Next free migration: **0040** (0039 -> 0040).

## Scope / non-scope
Scope: `clipos_api/analytics.py`, migration 0040, `tests/test_analytics.py`,
main.py wiring, test_migrations, docs/development.md.
Non-scope: real platform analytics SDKs, experiments/cohorts (PRD §H
advanced features), learned rules, prompt/model performance tracking.

## Dependencies and assumptions
- No dependencies on M14 publishing adapters (analytics is read-side).
- Uses the same Base/repository pattern as other modules.

## Files/domains expected to change
- New: `clipos_api/analytics.py`, `migrations/versions/0040_analytics.py`,
  `tests/test_analytics.py`, `.agent/plans/CLIP-062.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects; mock only. Audits: analytics_metrics.collected.
- Metric data is org-scoped and content-scoped.

## Implementation steps
1. analytics.py: AnalyticsAdapter Protocol (health_check, collect_metrics),
   MockAnalyticsAdapter (VIEW/LIKE/COMMENT deterministic), MetricKind enum,
   AnalyticsMetric/AnalyticsSnapshot contracts, AnalyticsMetricRecord table,
   AnalyticsStore (store idempotent per (org, content, kind, window),
   get with kind filter), collect_analytics flow, router (2 routes).
2. Migration 0040: analytics_metrics table.
3. test_analytics.py: mock adapter, store, collect flow, routes.
4. main.py: import analytics router.
5. test_migrations: add analytics_metrics.
6. development.md: analytics section.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, PG alembic
  0039 -> 0040, full pytest CI-env, OpenAPI probe, secret_scan,
  architecture_guard, state_audit verify.

## Completion evidence
Ticket, branch, files, migration 0040, gates + results, suite count,
commit SHAs, CI run id.