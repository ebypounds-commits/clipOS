# ExecPlan — CLIP-055 image/video generation adapters

## Ticket and objective
CLIP-055 (M13): provider-neutral image/video generation Protocols with
deterministic Mocks (no provider identifiers — registry alias is
provenance only), `image_assets` persistence, video through the CLIP-038
media chain (asset + v1 version with checksum). No LLM.

## Current repository facts
- Head at plan time: `ab8af1d` (CLIP-054 state advance). CI green.
- CLIP-054 TTS pattern; CLIP-038 `MediaIngestionStore.register_asset` /
  `record_version` (idempotent, monotonic max+1, conflict semantics
  verified by reading source). Next free migration: **0036**.
- Worktree `/srv/clipos/worktrees/CLIP-055`, branch `agent/CLIP-055`,
  venv ready, lease `mimo-clipos-m1-001` active.

## Scope / non-scope
Scope: `generation_adapters.py`, migration 0036,
`tests/test_generation_adapters.py`, main.py wiring, test_migrations,
development.md.
Non-scope: real provider SDKs (later tickets behind the Protocols),
stock/b-roll adapters (CLIP-056), AI-label metadata (CLIP-057), image
editing/upscaling.

## Dependencies and assumptions
- Depends on CLIP-054 (DONE). Mock video needs ffmpeg (skipif in tests).
  Idempotency keys: image id + brief/prompt/alias/dims/path; video id +
  title/storage_ref. Brief gate 404; confinement 422; conflict 409.

## Files/domains expected to change
- New: `clipos_api/generation_adapters.py`,
  `migrations/versions/0036_image_assets.py`,
  `tests/test_generation_adapters.py`, `.agent/plans/CLIP-055.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects beyond confined local writes + ffmpeg lavfi.
  Audits: image_assets.generated, video_generations.generated (+ store's
  asset/version audits). Mock temp dir is adapter-internal (system tmp,
  auto-cleaned); durable outputs always confined.

## Implementation steps
1. Protocols + pure PNG helpers (prompt_color, solid_png) + mocks.
2. ImageAsset/ImageGenerationRequest/VideoGenerationRequest/Result
   contracts + image_assets row.
3. GenerationStore (get/list + brief gate) + generate_image/generate_video
   flows with idempotency/conflict semantics.
4. Routes (5) with `_map_error`.
5. Migration 0036; wire main.py; test_migrations; docs.
6. Tests (sketch above).

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG 0035 -> 0036, OpenAPI probe, secret_scan,
  architecture_guard, state_audit verify.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed; validators guard state.

## Completion evidence
Ticket, branch, files, migration 0036, gates + results, suite count,
commit SHAs, CI run id.

## Remaining risks / follow-ups
- CLIP-056 stock/b-roll adapters; CLIP-057 AI-label provenance (M13
  remainder: 056, 057).
- Next free migration: 0037.
