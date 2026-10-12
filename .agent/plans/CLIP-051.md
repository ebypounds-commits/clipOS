# ExecPlan — CLIP-051 FFmpeg worker/render pipeline

## Ticket and objective
CLIP-051 (M12): execute CLIP-050 render specs — pure argv builder, runner
protocol with a real timeout-bounded subprocess backend, render_jobs
lifecycle with workdir-confined execution and deterministic manifests.
No LLM, no provider identifiers, no shell interpolation.

## Current repository facts
- Head at plan time: `674d0e6` (CLIP-050 state advance). CI green.
- ffmpeg 8.0.1 at /usr/bin/ffmpeg on the VPS; GitHub runners ship ffmpeg
  (CLIP-039 proved the pattern). Next free migration: **0032**.
- CLIP-039 lessons: allow_module_level skips avoided via per-test skipif;
  path safety policy from CLIP039_PATH_SAFETY_REVIEW applies.
- Worktree `/srv/clipos/worktrees/CLIP-051`, branch `agent/CLIP-051`,
  venv ready, lease `mimo-clipos-m1-001` active.

## Scope / non-scope
Scope: `render_pipeline.py`, migration 0032, `tests/test_render_pipeline.py`,
main.py wiring, test_migrations list, development.md.
Non-scope: caption burn-in and audio polish rendering (CLIP-052), aspect
variant matrix + QC + output manifests fan-out (CLIP-053), storage-layer
upload (local workdir only), Temporal orchestration of jobs.

## Dependencies and assumptions
- Depends on CLIP-050 (DONE). RENDER_WORKDIR default /tmp/clipos-render;
  outputs must be .mp4; PENDING and FAILED execute, SUCCEEDED/RUNNING 409.
- Spec captions flag recorded in manifest; loudnorm executed here (cheap,
  spec-driven); duration/aspect straight from spec (validated at
  registration).

## Files/domains expected to change
- New: `clipos_api/render_pipeline.py`,
  `migrations/versions/0032_render_jobs.py`,
  `tests/test_render_pipeline.py`, `.agent/plans/CLIP-051.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- External side effect: local ffmpeg subprocess — timeout-bounded,
  argv-only (no shell), workdir-confined paths (traversal defense).
- Audits: `render_jobs.created`, `render_jobs.succeeded`,
  `render_jobs.failed`.

## Implementation steps
1. Pure helpers: spec_fingerprint, ASPECT_FILTERS, build_render_argv,
   confine_path, render_workdir.
2. Runner protocol + SubprocessRenderRunner (timeout, stderr tail).
3. render_jobs row + store: create (project gate, idempotent,
   fingerprint capture), get/list, execute_job (status gates, confinement,
   argv build, run, manifest/error persistence, audits).
4. Routes (4) with `_map_error`.
5. Migration 0032; wire main.py; test_migrations; docs.
6. Tests: pure helpers, lifecycle with MockRunner, real-ffmpeg
   integration via testsrc (skipif), route probes incl. 422 traversal.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG 0031 -> 0032, OpenAPI probe, secret_scan,
  architecture_guard, state_audit verify.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed; validators guard state.
Real-render tests skip cleanly where ffmpeg is absent.

## Completion evidence
Ticket, branch, files, migration 0032, gates + results, suite count,
commit SHAs, CI run id.

## Remaining risks / follow-ups
- CLIP-052 captions/audio normalization rendering; CLIP-053 variants/QC.
- Long renders will eventually need Temporal (M3 worker) — current
  synchronous execute is bounded by RENDER_FFMPEG_TIMEOUT_SECONDS.
- Next free migration: 0033.
