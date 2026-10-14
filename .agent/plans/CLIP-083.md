# ExecPlan — CLIP-083 Agent Architect proposal generator

## Ticket and objective
CLIP-083 (M20): deterministic proposal generation from weakness
observations — derives proposal/evidence ids from sha1, composes the
CLIP-082 store, replay-safe, no new tables.

## Current repository facts
- Head at plan time: `e219147` (CLIP-082 state advance, 88/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-083`, branch `agent/CLIP-083`,
  venv ready, lease active. No migration needed (composes
  improvement_proposals + proposal_evidence).

## Scope / non-scope
Scope: `clipos_api/architect.py`, `tests/test_architect.py`, main.py
router wiring, docs/development.md.
Non-scope: LLM generation, new persistence, eval design artifacts,
auto-ticket creation.

## Implementation
1. derive_proposal_id / derive_evidence_id pure sha1 rules.
2. generate_proposal: register (409 on reworded identity), link
   evidence with created/replayed counts, audit generated.
3. POST /improvement-proposals/generate route; docs.

## Test/gate plan
~10 tests; ruff/mypy; pytest SQLite + CI-env (no PG migration);
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (88->89, current -> CLIP-084)
-> push -> CI green -> release lease, remove worktree, local pull.