# ExecPlan — CLIP-054 TTS provider adapter

## Ticket and objective
CLIP-054 (M13 opener): provider-neutral TTS Protocol + deterministic
Mock adapter (stdlib WAV silence), voice_assets persistence with required
registry `model_alias`, confined synthesis flow. No LLM, no vendor SDK,
no provider identifiers.

## Current repository facts
- Head at plan time: `75ffa3f` (CLIP-053 state advance, M12 closed). CI green.
- CLIP-040 precedent: TranscriptionAdapter Protocol + deterministic Mock,
  no vendor SDK. CLIP-051/052: RENDER_WORKDIR confinement helpers to
  reuse (`confine_path`). Next free migration: **0035**.
- Worktree `/srv/clipos/worktrees/CLIP-054`, branch `agent/CLIP-054`,
  venv ready, lease `mimo-clipos-m1-001` active.

## Scope / non-scope
Scope: `voice_assets.py`, migration 0035, `tests/test_voice_assets.py`,
main.py wiring, test_migrations, development.md.
Non-scope: real TTS provider SDKs/dispatch (later M13 tickets behind the
same Protocol), voice cloning, audio post-processing, storage upload.

## Dependencies and assumptions
- Depends on CLIP-053 (DONE). Mock output = valid WAV silence; duration
  15 chars/sec clamped 0.5..15s; synthesis paths confined to the shared
  render workdir; model_alias never defaulted.

## Files/domains expected to change
- New: `clipos_api/voice_assets.py`,
  `migrations/versions/0035_voice_assets.py`,
  `tests/test_voice_assets.py`, `.agent/plans/CLIP-054.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects (mock only); path confinement on write;
  audits: voice_assets.registered, voice_synthesis.completed.

## Implementation steps
1. Pure helpers (synthesis_duration_ms, silence_wav) + TTSAdapter
   Protocol + MockTTSAdapter.
2. VoiceAsset contracts (model_alias required) + SQL row.
3. Store (idempotent register/conflict, get/list) + synthesize_speech.
4. Routes (4) with `_map_error`.
5. Migration 0035; wire main.py; test_migrations; docs.
6. Tests: pure WAV validity (wave round-trip), duration bounds, mock
   determinism, store idempotency/conflict/audit, pydantic alias
   required, synthesis write+audit, 404/409/422 routes.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG 0034 -> 0035, OpenAPI probe, secret_scan,
  architecture_guard, state_audit verify.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed; validators guard state.

## Completion evidence
Ticket, branch, files, migration 0035, gates + results, suite count,
commit SHAs, CI run id.

## Remaining risks / follow-ups
- CLIP-055 image/video generation adapters next; provider dispatch by
  model_alias is the seam to preserve.
- Next free migration: 0036.
