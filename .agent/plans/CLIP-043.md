# ExecPlan — CLIP-043 multi-factor clip scoring

## Ticket and objective
CLIP-043 (M10): deterministic multi-factor scoring for clip candidates. Pure
component scorers + weighted total, `clip_scores` persistence (migration 0024),
idempotent score recording with NEW→SCORED transition, operator routes with
audit. No LLM, no provider identifiers.

## Current repository facts
- Head at plan time: `8e825ac` (Phase-0 housekeeping). CI green.
- CLIP-042 landed: `clip_candidates` (status NEW→SCORED→APPROVED/REJECTED),
  migration 0023. CLIP-036 pattern: `score_opportunity` weighted-sum, idempotent
  `record_score`, Depends-before-decorator routes.
- Next free migration: **0024** (chain: 0021 → 0023 → 0024).
- Relevant tables: `clip_candidates`, `semantic_segments` (kind HOOK/CLAIM,
  start_ms/end_ms, media_asset_id, org_id), `transcripts`/`transcript_segments`
  (text, start_ms/end_ms, transcript_id, org_id).
- Worktree: `/srv/clipos/worktrees/CLIP-043`, branch `agent/CLIP-043`,
  venv Python 3.14.4, lease `mimo-clipos-m1-001` (active-takeover, expires
  2026-10-12T13:46Z).

## Scope / non-scope
Scope: `clip_scores.py` module (pure scorers, store, flow, router), migration
0024, `tests/test_clip_scores.py`, `main.py` router wiring,
`test_migrations.py` table list, `docs/development.md` section.
Non-scope: review/approve workflow (CLIP-045), fingerprint dedup (CLIP-044),
LLM-based scoring, model registry wiring beyond the optional `scorer_alias`
reference column, web UI.

## Dependencies and assumptions
- Depends on CLIP-042 (DONE). Assumes org-scoped reads on all tables.
- Weights are caller-supplied policy (like CLIP-036 thresholds); factor names
  validated against `COMPONENT_FACTORS`.
- `scorer_alias` is a free registry-style reference — validation is length only;
  the vendor scan (`PROVIDER_PATTERN`) is the guard against vendor strings.

## Files/domains expected to change
- New: `services/api/clipos_api/clip_scores.py`, `migrations/versions/0024_clip_scores.py`,
  `services/api/tests/test_clip_scores.py`, `.agent/plans/CLIP-043.md`.
- Edit: `clipos_api/main.py` (import + extend routes), `tests/test_migrations.py`
  (add `clip_scores`), `docs/development.md` (module section).

## Security, rights, budget and external-side-effect impact
- No external side effects; all reads/writes are DB-local and org-scoped.
- No secrets, no rights implications, no budget impact.
- Audit events: `clip_scores.recorded`, `clip_candidate.scored`.

## Implementation steps
1. Module: pure scorers (`length_fit`, `span_coverage`, `density_score`,
   `score_clip`, `compute_components`) — unit-testable in isolation.
2. Contracts: frozen Pydantic `ClipScoreRequest`/`ClipScore`; SQL row
   `ClipScoreRecord` (unique (org_id, candidate_id), unique score_id).
3. Store: `load_scoring_context` (candidate + HOOK/CLAIM spans + word count,
   org-scoped, decided candidates frozen), `record_score` (idempotent,
   NEW→SCORED, dual audit), `get_score`, `list_by_org`.
4. Flow `score_candidate`: validate weights → context → components → total →
   record. ValueError messages designed for 404/409/422 route mapping.
5. Router: providers BEFORE decorators; POST/GET score routes.
6. Migration 0024 (revises 0023, CREATE-only, three indexes).
7. Wire `main.py`; add table to `test_migrations.py`.
8. Docs section in `development.md`.

## Test/gate plan
- `ruff format --check services/api`
- `ruff check services/api`
- `mypy services/api/clipos_api`
- `pytest` full suite (SQLite in-memory; PG-gated tests when DATABASE_URL set)
- `alembic upgrade head` against compose PG 5433 (0023 → 0024, idempotent)
- Route probe via real TestClient requests (PEP 563 rule), not import-only.
- Offline SQL render test asserts `CREATE TABLE clip_scores`.
- OpenAPI export (`app.openapi()` build) — no TYPE_CHECKING aliases used.

## Rollback / recovery plan
Migration is CREATE-only; rollback = revert merge commit and drop table via
downstream ops if needed. State advance reverts via `validate_state` guards.
Worktree is isolated; canonical main only moves after gates pass.

## Completion evidence
Completion report: ticket, branch/worktree, files changed, migration id 0024,
gates+results, suite count, commit SHAs, CI run id on final push.

## Remaining risks / follow-ups
- CLIP-044 must not reuse `content_fingerprints` naming (M7 already has a
  `content_fingerprints` table — candidate fingerprinting must link, not
  duplicate).
- Review UI (CLIP-045) will consume `GET /clip-scores`.
- If parallel dispatch returns later, migration 0025 is next free id.
