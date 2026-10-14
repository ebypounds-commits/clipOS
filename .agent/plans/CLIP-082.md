# ExecPlan — CLIP-082 Improvement proposal evidence model

## Ticket and objective
CLIP-082 (M20 opener): improvement proposals with a guarded review
lifecycle and append-only evidence links (PRD journey 5).

## Current repository facts
- Head at plan time: `8d542e9` (CLIP-081 state advance, 87/102, M19
  closed, M20 current). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-082`, branch `agent/CLIP-082`,
  venv ready, lease active. Next free migration: **0060** (0059 -> 0060).

## Scope / non-scope
Scope: `clipos_api/improvement.py`, migration 0060
(improvement_proposals + proposal_evidence), tests, main.py wiring,
test_migrations, docs/development.md.
Non-scope: evaluation design artifacts, auto-ticket generation,
canary execution.

## Implementation
1. Pure proposal_transition_allowed (full journey to VERIFIED /
   ROLLED_BACK / REJECTED; no skips; same-state noop).
2. ImprovementStore: register idempotent/conflict/audited; transitions
   audited; evidence link (proposal 404, refs only, idempotent/
   conflict, audited); list filters.
3. Migration 0060; 6 routes; docs.

## Test/gate plan
~16 tests; ruff/mypy; pytest SQLite + CI-env; PG 0059->0060;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (87->88, current -> CLIP-083)
-> push -> CI green -> release lease, remove worktree, local pull.