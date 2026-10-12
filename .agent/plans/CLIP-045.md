# ExecPlan — CLIP-045 review UI and benchmark eval set

## Ticket and objective
CLIP-045 (M10, final ticket): operator review tooling — APPROVE/REJECT
decision API with status transitions, candidate response views carrying
`candidate_id`/`status`, a `/reviews` web page (score / dup-check / decide),
and a frozen benchmark eval set for the deterministic M10 helpers.

## Current repository facts
- Head at plan time: `94065d1` (CLIP-044 state advance). CI green.
- Landed: CLIP-042 candidate lifecycle (NEW→SCORED→APPROVED/REJECTED enum,
  no decision route yet), CLIP-043 scoring (freezes decided candidates),
  CLIP-044 dedup findings. Next free migration: **0026** (0025 → 0026).
- Web: Next.js 16.3.8, React 19, Tailwind, app router; pages use
  `export const dynamic = "force-dynamic"` + `NEXT_PUBLIC_API_BASE`; home
  nav has agents/models links. Node v22.23.1 + npm 10.9.8 on VPS.
- `tests/fixtures/` precedent: `reference_workflow_history.json`.
- Worktree `/srv/clipos/worktrees/CLIP-045`, branch `agent/CLIP-045`, venv
  Python 3.14.4, lease `mimo-clipos-m1-001` expires 2026-10-12T14:57Z.

## Scope / non-scope
Scope: `clip_review.py`, migration 0026, `ClipCandidateView` response
extension, `tests/test_clip_review.py`, benchmark fixture + runner,
`app/reviews/*`, home nav link, main.py wiring, test_migrations list,
development.md section.
Non-scope: rights/content publication review (separate gates), persisted
finding history, near-dup ML, auth/RBAC on review routes (org-scoped reads
only; RBAC enforcement is identity-milestone work), analytics of decisions.

## Dependencies and assumptions
- Depends on CLIP-044 (DONE). Decisions allowed from any non-frozen state;
  reversal (APPROVE then REJECT) is structurally allowed like CLIP-036
  (unique per (candidate, decision kind)).
- Scoring already freezes APPROVED/REJECTED — order of operations in UI:
  score → dup-check → decide.
- Benchmark expectations hand-computed; sha256 values generated with
  canonical hashlib (independent of module code).

## Files/domains expected to change
- New: `clipos_api/clip_review.py`, `migrations/versions/0026_candidate_decisions.py`,
  `tests/test_clip_review.py`, `tests/fixtures/clip_benchmark.json`,
  `tests/test_clip_benchmark.py`, `apps/web/app/reviews/{page.tsx,ReviewBoard.tsx,types.ts}`,
  `.agent/plans/CLIP-045.md`.
- Edit: `clipos_api/clip_candidates_router.py` (view model),
  `clipos_api/main.py`, `tests/test_migrations.py`, `apps/web/app/page.tsx`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects; org-scoped; no secrets. Audit:
  `clip_candidates.decided`.
- UI actors are free-text `actor_ref` — true identity/RBAC binding is
  identity-milestone scope (documented assumption).

## Implementation steps
1. `clip_review.py`: contracts, `candidate_decisions` row, store with
  idempotent record + status transition + audit, routes (providers before
  decorators).
2. Migration 0026 (revises 0025, CREATE-only, three indexes).
3. `ClipCandidateView` + `_view()` helper; switch 4 response models.
4. Benchmark fixture (9 score / 6 overlap / 5 fingerprint cases) + runner
  test with minimum-count guard.
5. Web: types, server page (`await searchParams`, `NEXT_PUBLIC_ORG_ID`
  fallback), client `ReviewBoard` (score/dup/decide actions, org field,
  error surfacing, router.refresh), home nav link.
6. Wire main.py; test_migrations += candidate_decisions; docs section.

## Test/gate plan
- `ruff format --check`, `ruff check`, `mypy` (CI + `--python-version 3.12`).
- Full pytest SQLite; full pytest CI-env (PG/Redis/MinIO/Temporal).
- `alembic upgrade head` PG (0025 → 0026, idempotent).
- Real TestClient probes (PEP 563) incl. view fields + decision roundtrip.
- Web: `npm install --no-audit --no-fund`, `npx tsc --noEmit`, `npm run build`.
- OpenAPI export; `secret_scan`, `architecture_guard`, `state_audit verify`.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed. State advance guarded by
validators. Web is additive (new page + additive response fields).

## Completion evidence
Ticket, branch/worktree, files, migration 0026, gates + results, suite
counts (python + tsc + build), commit SHAs, CI run id on final push.

## Remaining risks / follow-ups
- M11 starts after this lands (research/claims engine, CLIP-046).
- `actor_ref` should later bind to real identities (M1 sessions/RBAC).
- Benchmark fixture version bumps require deliberate review (frozen v1).
- Next free migration: 0027.
