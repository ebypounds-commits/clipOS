# ExecPlan — CLIP-085 Codex engineering-ticket generation/export

## Ticket and objective
CLIP-085 (M20 closer): turn accepted CLIP-082 proposals into
validator-clean Codex task packets with lifecycle + markdown export
(PRD journey 5 hand-off).

## Current repository facts
- Head at plan time: `506ac18` (provider wiring format fix; CLIP-084
  state 90/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-085`, branch `agent/CLIP-085`,
  venv ready, lease active. Next free migration: **0062** (0061 -> 0062).
- `.agent/CODEX_TASK_PACKET_TEMPLATE.md` + scripts/validate_packet.py
  define the required headings/lease keys (validator importable in
  tests).

## Scope / non-scope
Scope: `clipos_api/codex_tickets.py`, migration 0062, tests,
main.py wiring, test_migrations, docs/development.md.
Non-scope: actual Codex/worker dispatch, LLM packet authoring,
packet schemas changes.

## Implementation
1. Pure generate_packet_markdown (all validator headings, proposal-
   derived objective/acceptance/evidence) + ticket_transition_allowed.
2. Store: generate (proposal 404, ACCEPTED/IMPLEMENTED 409, idempotent
   conflict, audited generated), get/list, transition audited.
3. Routes: POST/GET /engineering-tickets, GET .../{id}, GET
   .../{id}/export (text/markdown attachment), POST .../transition.
4. Migration 0062; tests incl. validate_packet acceptance of generated
   packets; docs.

## Test/gate plan
~13 tests; ruff/mypy; pytest SQLite + CI-env; PG 0061->0062;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (90->91, M20 COMPLETE,
current_milestone M21, current -> CLIP-086) -> push -> CI green ->
release lease, remove worktree, local pull.