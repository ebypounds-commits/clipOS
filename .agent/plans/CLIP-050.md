# ExecPlan — CLIP-050 render project DSL

## Ticket and objective
CLIP-050 (M12 opener): declarative render project documents with a pure
spec validator (the DSL), persistence of validated specs, and a dry-run
validation endpoint. No FFmpeg execution (CLIP-051), no LLM, no provider
identifiers.

## Current repository facts
- Head at plan time: `a8aeeda` (CLIP-049 state advance, M11 closed). CI green.
- CLIP-049 landed evals/transformations (migration 0030); briefs exist
  (CLIP-047) as the render gate. Next free migration: **0031**.
- M12 scope: FFmpeg compositions, captions, audio normalization, aspect
  variants, deterministic render manifests (09_MILESTONES).
- Worktree `/srv/clipos/worktrees/CLIP-050`, branch `agent/CLIP-050`,
  venv ready, lease `mimo-clipos-m1-001` active.

## Scope / non-scope
Scope: `render_project.py`, migration 0031, `tests/test_render_project.py`,
main.py wiring, test_migrations list, development.md.
Non-scope: render_jobs/outputs tables and FFmpeg execution (CLIP-051),
captions track rendering, platform upload, QC reports.

## Dependencies and assumptions
- Depends on CLIP-049 (DONE). Spec version frozen at 1; aspect set
  {9:16, 16:9, 1:1}; duration 1s..10min; outputs 1..8 unique platform
  slugs; unknown top-level keys rejected.
- Registration validates before persistence (fail-closed); the dry-run
  endpoint is pure (no DB).

## Files/domains expected to change
- New: `clipos_api/render_project.py`,
  `migrations/versions/0031_render_projects.py`,
  `tests/test_render_project.py`, `.agent/plans/CLIP-050.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects; org-scoped; no secrets. Audit:
  `render_projects.registered`.

## Implementation steps
1. Pure `validate_render_spec` (documented bounds above).
2. Contracts: RenderProject, RenderSpecValidateRequest, ValidationResult.
3. SQL row + unique/lookup indexes.
4. Store: register (validate -> brief gate -> idempotent/conflict),
   get/list.
5. Routes (4) incl. pure dry-run endpoint; `_map_error`.
6. Migration 0031; wire main.py; test_migrations; docs.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG 0030 → 0031, OpenAPI probe, secret_scan,
  architecture_guard, state_audit verify.
- TestClient probes: dry-run (valid/invalid), register+fetch roundtrip,
  422 invalid spec with message, 404 brief, 409 conflict.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed; validators guard state.

## Completion evidence
Ticket, branch, files, migration 0031, gates + results, suite count,
commit SHAs, CI run id.

## Remaining risks / follow-ups
- CLIP-051 (FFmpeg worker/render pipeline) consumes these specs — the
  heaviest ticket of M12.
- Next free migration: 0032.
