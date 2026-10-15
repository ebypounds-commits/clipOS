# ExecPlan — CLIP-087 Incident lifecycle and runbooks

## Ticket and objective
CLIP-087 (M21): declared incident response — runbooks with numbered
steps, incidents with a guarded lifecycle bound to a matching
runbook, and an append-only timeline (PRD §K).

## Current repository facts
- Head at plan time: `980feb2` (CLIP-086 state advance, 92/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-087`, branch `agent/CLIP-087`,
  venv ready, lease active. Next free migration: **0064** (0063 -> 0064).

## Scope / non-scope
Scope: `clipos_api/incidents.py`, migration 0064 (runbooks,
runbook_steps, incidents, incident_timeline), tests, main.py wiring,
test_migrations, docs/development.md.
Non-scope: auto-remediation (CLIP-088), alerting/notifications,
on-call scheduling.

## Implementation
1. Pure incident_transition_allowed (OPEN -> ACKNOWLEDGED ->
   MITIGATED -> RESOLVED -> CLOSED; dismiss from ACK/MITIGATED;
   regression reopen MITIGATED -> OPEN; CLOSED terminal).
2. IncidentStore: runbook register (steps append-only, idempotent/
   conflict, audited), incident open (runbook 404 + category match
   409, seeds timeline, audited), transitions (record a timeline
   entry, audited), timeline append (404/409, audited), list filters.
3. Migration 0064; 8 routes; docs.

## Test/gate plan
~15 tests; ruff/mypy; pytest SQLite + CI-env; PG 0063->0064;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (92->93, current -> CLIP-088)
-> push -> CI green -> release lease, remove worktree, local pull.