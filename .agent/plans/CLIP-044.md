# ExecPlan — CLIP-044 duplicate/content fingerprint checks

## Ticket and objective
CLIP-044 (M10): deterministic duplicate detection for clip candidates —
window-overlap plus transcript-content fingerprints, persisted as facts,
findings derived on demand, fully audited. No LLM, no provider identifiers.

## Current repository facts
- Head at plan time: `8edd41d` (CLIP-043 state advance). CI green.
- CLIP-043 landed: `clip_scores` + migration 0024; CLIP-032 built manifest-level
  `content_fingerprints` (provenance scope — must NOT be duplicated).
- Next free migration: **0025** (chain: 0023 → 0024 → 0025).
- Relevant tables: `clip_candidates` (org, media_asset_id, start_ms, end_ms),
  `transcripts` (org, media_asset_id, created_at), `transcript_segments`
  (transcript_id, org, seq, start_ms, end_ms, text).
- Worktree `/srv/clipos/worktrees/CLIP-044`, branch `agent/CLIP-044`, venv
  Python 3.14.4, lease `mimo-clipos-m1-001` expires 2026-10-12T14:34Z.
- Cycle hazard: `transcription_store <-> transcription` (CLIP-043 fix:
  deferred import inside the method that needs the tables).

## Scope / non-scope
Scope: `clip_dedup.py` module (pure helpers, contracts, store, flow, router),
migration 0025, `tests/test_clip_dedup.py`, `main.py` wiring,
`test_migrations.py` table list, `docs/development.md` section.
Non-scope: byte-level media hashing (object storage reads), manifest
provenance fingerprints (CLIP-032), review actions on findings (CLIP-045),
fuzzy near-dup ML matching.

## Dependencies and assumptions
- Depends on CLIP-043 (DONE). Threshold is caller-supplied policy (default
  0.5, gt=0 le=1 via pydantic), like CLIP-036 thresholds.
- Primary transcript = earliest (created_at, transcript_id) for the asset —
  deterministic pick keeps language mixing out of content hashes.
- Empty normalized text → no TRANSCRIPT row (absence is not content evidence).

## Files/domains expected to change
- New: `services/api/clipos_api/clip_dedup.py`,
  `migrations/versions/0025_candidate_fingerprints.py`,
  `services/api/tests/test_clip_dedup.py`, `.agent/plans/CLIP-044.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects; org-scoped reads/writes only. No secrets.
- Audit events: `candidate_fingerprints.persisted`,
  `clip_candidates.duplicate_check`.
- Findings are advisory (no candidate state mutation) — review/decision is
  CLIP-045 scope, so no rights/budget impact.

## Implementation steps
1. Pure helpers: `normalize_text`, `overlap_over_shorter`,
   `window_fingerprint`, `transcript_fingerprint` (None on empty).
2. Contracts: `CandidateFingerprint` (algorithm Literal["sha256"]),
   `DuplicateCheckRequest`, `DuplicateFinding`, `DuplicateCheckReport`.
3. SQL row `CandidateFingerprintRecord` (unique (org, candidate, kind);
   lookup index (org, kind, value)).
4. Store: `load_candidate` (404), `persist_fingerprint` (idempotent, conflict
  raise), `list_fingerprints`, `list_fingerprints_by_value` (exclude self),
  `list_other_candidates_on_asset`, `list_candidates_by_ids`.
5. Flow `check_candidate_duplicates`: threshold validation → candidate load →
  persist WINDOW (always) + TRANSCRIPT (when text) → window findings
  (same asset, overlap >= threshold) → content findings (org-wide value
  collision) → audit → report.
6. Router: providers BEFORE decorators; POST duplicate-check (404/409/422),
  GET fingerprints (404 unknown).
7. Migration 0025 (revises 0024, CREATE-only, three indexes).
8. Wire `main.py`; add table to `test_migrations.py`; docs section.

## Test/gate plan
- `ruff format --check`, `ruff check`, `mypy` (CI invocation +
  `--python-version 3.12`).
- Full pytest SQLite + full pytest CI-env (PG/Redis/MinIO/Temporal).
- `alembic upgrade head` against compose PG (0024 → 0025, idempotent).
- Real TestClient POST/GET probes (PEP 563 rule); OpenAPI export build.
- `secret_scan.sh`, `architecture_guard.py`, `state_audit.py verify`.

## Rollback / recovery plan
CREATE-only migration; revert merge commit if needed. State advance guarded
by validate_state/validate_handoff. Worktree isolated; canonical main only
moves after gates pass.

## Completion evidence
Completion report: ticket, branch/worktree, files, migration 0025, gates +
results, suite count, commit SHAs, CI run id on final push.

## Remaining risks / follow-ups
- CLIP-045 (review UI) consumes findings via POST duplicate-check and GET
  fingerprints; may request persisted findings if UI needs history.
- Byte-level media fingerprints remain future work (object-storage reads,
  M12+ render pipeline is the natural home).
- Next free migration after this ticket: 0026.
