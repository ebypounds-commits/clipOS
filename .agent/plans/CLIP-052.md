# ExecPlan — CLIP-052 captions/audio normalization/branding

## Ticket and objective
CLIP-052 (M12): caption track persistence + SRT generation, ffmpeg
burn-in and logo overlay integration into the CLIP-051 pipeline, with
audio normalization remaining exercised alongside. No LLM, no provider
identifiers.

## Current repository facts
- Head at plan time: `cc3fe6b` (CLIP-051 state advance). CI green.
- CLIP-051: build_render_argv (trim/aspect/loudnorm), execute_job with
  confinement + manifest, render_jobs lifecycle. ffmpeg 8.0.1 on VPS with
  libass (subtitles filter available). Next free migration: **0033**.
- Worktree `/srv/clipos/worktrees/CLIP-052`, branch `agent/CLIP-052`,
  venv ready, lease `mimo-clipos-m1-001` active.

## Scope / non-scope
Scope: new `caption_tracks.py`, edits to `render_pipeline.py` (argv
extension + execute prep), migration 0033, `tests/test_caption_tracks.py`
+ pipeline test additions, main.py wiring, test_migrations, development.md.
Non-scope: caption style rendering (style_ref recorded only), WebVTT
outputs, CLIP-053 aspect-variant fanout/QC, storage upload.

## Dependencies and assumptions
- Depends on CLIP-051 (DONE). Latest track wins (created_at, id desc).
  captions enabled + no track -> clean render (captions_burned=false).
  logo_ref is workdir-relative; missing/escaping -> 422 before RUNNING.
- Existing CLIP-051 argv tests must pass unchanged (defaults None).

## Files/domains expected to change
- New: `clipos_api/caption_tracks.py`,
  `migrations/versions/0033_caption_tracks.py`,
  `tests/test_caption_tracks.py`, `.agent/plans/CLIP-052.md`.
- Edit: `clipos_api/render_pipeline.py`, `clipos_api/main.py`,
  `tests/test_migrations.py`, `tests/test_render_pipeline.py`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- ffmpeg still argv-only/no shell; all new paths workdir-confined; SRT
  written inside RENDER_WORKDIR. Audits: `caption_tracks.registered`
  (plus CLIP-051 job audits unchanged).

## Implementation steps
1. caption_tracks module: CaptionSegment (ordered window validator),
   validate_caption_segments, segments_to_srt, store, 3 routes.
2. render_pipeline: escape_filter_path; build_render_argv kwargs
   (merged vf / filter_complex overlay); execute prep (SRT write + logo
   confine pre-RUNNING); manifest flags.
3. Migration 0033; wire main.py; test_migrations += caption_tracks.
4. Tests: pure SRT/validation, store/routes, argv extensions, execute
   integration (mock + real burn-in skipif), logo missing/escape 422 with
   job left PENDING.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG 0032 -> 0033, OpenAPI probe, secret_scan,
  architecture_guard, state_audit verify.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed; validators guard state.

## Completion evidence
Ticket, branch, files, migration 0033, gates + results, suite count,
commit SHAs, CI run id.

## Remaining risks / follow-ups
- CLIP-053 (aspect variants/QC/render manifests) closes M12 after this.
- libass availability assumed on CI runners (Ubuntu distro ffmpeg) —
  real burn-in test will surface it immediately.
- Next free migration: 0034.
