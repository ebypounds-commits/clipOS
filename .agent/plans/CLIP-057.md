# ExecPlan — CLIP-057 generated-media provenance/AI-label metadata

## Ticket and objective
CLIP-057 (M13 final): AI-label disclosure rows for generated media +
transformation provenance linkage (CLIP-049 table) for brief-linked
generations, auto-applied at the generation routes plus an explicit
metadata endpoint. No LLM, no provider identifiers (generator_alias is
a required registry ref). Closes M13.

## Current repository facts
- Head at plan time: `df1be38` (CLIP-056 state advance). CI green.
- CLIP-054 TTS + CLIP-055 image/video routes exist; CLIP-049
  transformation_records (brief-gated, idempotent per
  (org, transformation_id)); M7 MediaAssetKind enum reusable. Next free
  migration: **0037** (0036 -> 0037).
- Worktree `/srv/clipos/worktrees/CLIP-057`, branch `agent/CLIP-057`,
  venv ready, lease `mimo-clipos-m1-001` active.

## Scope / non-scope
Scope: new `ai_labels.py`, edits to `voice_assets.py` +
`generation_adapters.py` routes (auto-label), migration 0037,
`tests/test_ai_labels.py`, main.py wiring, test_migrations, development.md.
Non-scope: web UI disclosure surfaces, platform-specific label APIs
(M14+), retroactive labeling of pre-057 artifacts, changing flow-level
signatures (route-level integration chosen — flows untouched).

## Dependencies and assumptions
- Depends on CLIP-056 (DONE). Brief validated BEFORE any persist (no
  partial state). Voice (no brief) -> label only; image/video (brief)
  -> label + transformation. Deterministic ids give end-to-end
  idempotency across repeat POSTs.

## Files/domains expected to change
- New: `clipos_api/ai_labels.py`,
  `migrations/versions/0037_ai_labels.py`, `tests/test_ai_labels.py`,
  `.agent/plans/CLIP-057.md`.
- Edit: `clipos_api/voice_assets.py`, `clipos_api/generation_adapters.py`,
  `clipos_api/main.py`, `tests/test_migrations.py`, `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects; org-scoped; audits ai_labels.recorded (+
  CLIP-049 store audits for transformations). generator_alias required
  everywhere (registry seam). Labels make synthetic-media disclosure
  enforceable — fail-closed (brief check before persist).

## Implementation steps
1. ai_labels module: contracts, deterministic id helpers, relative_ref,
   store (idempotent/conflict), record_generated_media (brief pre-check),
   3 routes.
2. Route integrations: voice synthesize (AUDIO label), image route
  (IMAGE label + transformation), video route (VIDEO label + transformation).
3. Migration 0037; wire main.py; test_migrations += ai_labels; docs.
4. Tests: id helpers, store idempotency/conflict/org-kind scoping/audit,
   transformation linkage (present/absent/missing brief), route probes
   incl. image auto-label + transformation roundtrip, 404/409 paths.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG 0036 -> 0037, OpenAPI probe, secret_scan,
  architecture_guard, state_audit verify.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed; validators guard state.
M13 closes on this land.

## Completion evidence
Ticket, branch, files, migration 0037, gates + results, suite count,
commit SHAs, CI run id.

## Remaining risks / follow-ups
- M14 (publishing connectors) starts next — CLIP-058 adapter contract,
  CLIP-059/060 YouTube/TikTok (mocks fine), CLIP-061 Meta needs current
  official-doc verification (web research — I can do that, not a human
  gate).
- Next free migration: 0038.
