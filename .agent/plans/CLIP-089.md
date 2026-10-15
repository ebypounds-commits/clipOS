# ExecPlan — CLIP-089 Credential/quota/disk/provider health monitors

## Ticket and objective
CLIP-089 (M21 closer): the four resource-exhaustion monitors from
PRD §K (credential expiry, quota headroom, disk usage, provider
health) as pure functions plus one append-only check ledger and a
single "what needs attention" summary.

## Current repository facts
- Head at plan time: `2536dcb` (CLIP-088 state advance, 94/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-089`, branch `agent/CLIP-089`,
  venv ready, lease active. Next free migration: **0066** (0065 -> 0066).
- CLIP-086 provides health_signals (live probes); this ticket adds the
  resource-specific monitors and attention aggregation.

## Scope / non-scope
Scope: `clipos_api/monitors.py`, migration 0066, tests, main.py wiring,
test_migrations, docs/development.md.
Non-scope: metric collection from external providers, alert routing,
prediction/forecasting.

## Implementation
1. Pure checks: credential expiry bands, quota headroom (invalid limit
   DOWN), disk usage (capacity unknown DOWN), provider health mapping.
2. evaluate_monitor dispatcher validating required inputs per kind.
3. summarize_monitors: latest per kind, worst rank, attention list.
4. MonitorStore record (idempotent/conflict/audited) + list filters.
5. Routes: POST /monitors/check, GET /monitors, GET /monitors/checks.
   Migration 0066; docs.

## Test/gate plan
~18 tests; ruff/mypy; pytest SQLite + CI-env; PG 0065->0066;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (94->95, M21 COMPLETE,
current_milestone M22, current -> CLIP-090) -> push -> CI green ->
release lease, remove worktree, local pull.