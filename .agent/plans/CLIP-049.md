# ExecPlan — CLIP-049 factuality/transformation evaluations

## Ticket and objective
CLIP-049 (M11 final): factuality evaluation records that drive claim
veracity through a deterministic PASS/FAIL/INCONCLUSIVE mapping, plus
transformation rights-chain records per 07_SECURITY. No LLM, no provider
identifiers.

## Current repository facts
- Head at plan time: `7294631` (CLIP-048 state advance). CI green.
- CLIP-048 landed hooks/scripts; CLIP-046 claims carry veracity;
  CLIP-047 briefs exist. Next free migration: **0030** (0029 → 0030).
- Patterns: client natural id idempotency, mapping table
  `VERACITY_FOR_RESULT` as module constant, audits with reason_ref chain.
- Worktree `/srv/clipos/worktrees/CLIP-049`, branch `agent/CLIP-049`, venv
  ready, lease `mimo-clipos-m1-001` active (acquired with this ticket).

## Scope / non-scope
Scope: `content_evals.py`, migration 0030 (factuality_evaluations,
transformation_records), `tests/test_content_evals.py`, main.py wiring,
test_migrations list, development.md.
Non-scope: agent evaluations (M20/agent_evaluation.py exists), QC reports,
render manifests (M12), LLM-based evaluation runs (method_ref can carry a
registry alias but no dispatch here), web UI.

## Dependencies and assumptions
- Depends on CLIP-048 (DONE). Eval veracity mapping is the ticket's
  engine: PASS→VERIFIED, FAIL→DISPUTED, INCONCLUSIVE→unchanged, applied
  atomically with the eval insert; reason_ref = eval_id.
- Transformation refs capped at 255 chars (PG btree entry limit on the
  unique index).

## Files/domains expected to change
- New: `clipos_api/content_evals.py`,
  `migrations/versions/0030_content_evals.py`,
  `tests/test_content_evals.py`, `.agent/plans/CLIP-049.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects; org-scoped; no secrets. Audits:
  `factuality_evaluations.recorded`, `claims.veracity_changed`,
  `transformation_records.registered`.
- Veracity changes affect the CLIP-047 planning gate (only VERIFIED
  claims plan) — that coupling is intentional and covered by tests.

## Implementation steps
1. Contracts + `VERACITY_FOR_RESULT` mapping.
2. SQL rows with unique/lookup indexes.
3. Store: record_evaluation (claim gate, idempotent, atomic mapping +
   dual audit), list_evaluations (404), register_transformation (brief
   gate, idempotent, conflict), list_transformations (404).
4. Routes (4) with `_map_error`.
5. Migration 0030 (revises 0029, CREATE-only).
6. Wire main.py; test_migrations += 2 tables; docs section.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG 0029 → 0030, OpenAPI probe, secret_scan,
  architecture_guard, state_audit verify.
- TestClient probes: eval roundtrip proving claim veracity flips over
  HTTP; transformation roundtrip; 404/409 mappings.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed; validators guard state.
M11 closes on this land.

## Completion evidence
Ticket, branch, files, migration 0030, gates + results, suite count,
commit SHAs, CI run id.

## Remaining risks / follow-ups
- M12 (media studio) starts after this — FFmpeg render pipeline, the
  heaviest engineering phase; transformation records will feed it.
- Next free migration: 0031.
