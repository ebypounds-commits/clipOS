# ExecPlan — CLIP-086 Health supervisor signals

## Ticket and objective
CLIP-086 (M21 opener): aggregated operator health view — live probes
(db/cache/storage/workflow), append-only signals, worst-rank overall
status (PRD §K service health).

## Current repository facts
- Head at plan time: `70df829` (CLIP-085 state advance, 91/102, M20
  closed, M21 current). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-086`, branch `agent/CLIP-086`,
  venv ready, lease active. Next free migration: **0063** (0062 -> 0063).

## Scope / non-scope
Scope: `clipos_api/health_supervisor.py`, migration 0063, tests,
main.py wiring, test_migrations, docs/development.md.
Non-scope: incident lifecycle (CLIP-087), auto-remediation (CLIP-088),
persistent alerting/notifications.

## Implementation
1. Pure summarize_signals (latest per subsystem, worst-rank overall).
2. Live probes never raise (SELECT 1 / Redis PING / MinIO health /
   Temporal port) with env-driven URLs; UNKNOWN when env unset.
3. HealthStore record (idempotent/conflict/audited), probe recording
   (uuid ids, source LIVE), summarize.
4. Routes: POST /health/probe, GET /health/signals, POST
   /health/signals. Migration 0063; docs.

## Test/gate plan
~13 tests; ruff/mypy; pytest SQLite + CI-env; PG 0062->0063;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (91->92, current -> CLIP-087)
-> push -> CI green -> release lease, remove worktree, local pull.