# ExecPlan — CLIP-053 platform aspect variants/QC/render manifests

## Ticket and objective
CLIP-053 (M12 final): per-platform aspect-variant fanout with ffprobe QC
and deterministic per-output manifests. Closes the automated media
studio milestone. No LLM, no provider identifiers.

## Current repository facts
- Head at plan time: `35e8c02` (CLIP-052 state advance). CI green.
- CLIP-051 pipeline + CLIP-052 captions/logo; ffmpeg/ffprobe 8.0.1 on
  VPS (libass verified by CLIP-052 real burn-in). Next free migration:
  **0034** (0033 -> 0034).
- CLIP-050 validator tolerates unknown nested output keys -> outputs[i]
  .aspect is registrable without changing CLIP-050; execute validates it.
- Worktree `/srv/clipos/worktrees/CLIP-053`, branch `agent/CLIP-053`,
  venv ready, lease `mimo-clipos-m1-001` active.

## Scope / non-scope
Scope: new `render_variants.py`, render_pipeline edits (EXPECTED
RESOLUTIONS, aspect_override kwarg, prep_job_assets extraction, SRT
confine fix, job_id slug pattern), migration 0034,
`tests/test_render_variants.py`, main.py wiring, test_migrations,
development.md.
Non-scope: platform_variants publishing definitions (M14), storage
upload, Temporal scheduling, changing CLIP-051 single-execute semantics.

## Dependencies and assumptions
- Depends on CLIP-052 (DONE). QC duration tolerance ±15%; exact
  resolution match; probe failure = FAILED report (fail-closed).
- Re-run after FAILED clears prior output/QC rows for the job.
- `prober` injectable for deterministic tests; real ffprobe path covered
  by skipif integration tests.

## Files/domains expected to change
- New: `clipos_api/render_variants.py`,
  `migrations/versions/0034_render_variants.py`,
  `tests/test_render_variants.py`, `.agent/plans/CLIP-053.md`.
- Edit: `clipos_api/render_pipeline.py`, `clipos_api/main.py`,
  `tests/test_migrations.py`, `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- All new paths workdir-confined; platform slugs regex-validated
  (filename injection defense); job_id slug-pattern at request time;
  ffprobe/ffmpeg argv-only with timeouts. Audits: render_outputs.rendered,
  qc_reports.recorded, render_jobs.succeeded/failed.

## Implementation steps
1. render_pipeline edits (constants, kwargs, prep extraction, fixes).
2. render_variants module: records, evaluate_qc (pure), probe_output
   (ffprobe), RenderVariantStore.execute_variants + list/get, routes.
3. Migration 0034; wire main.py; test_migrations += 2 tables.
4. Tests: QC pure matrix, argv override, slug/pattern guards, mock
   fanout success/QC-fail/ffmpeg-fail/status-gates/rerun-clears,
   route probes, real ffmpeg+ffprobe two-platform integration.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG 0033 -> 0034, OpenAPI probe, secret_scan,
  architecture_guard, state_audit verify.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed; validators guard state.
M12 closes on this land.

## Completion evidence
Ticket, branch, files, migration 0034, gates + results, suite count,
commit SHAs, CI run id.

## Remaining risks / follow-ups
- M13 (voice & generated media adapters) starts next; transformation
  records (CLIP-049) feed AI-label provenance there.
- Next free migration: 0035.
