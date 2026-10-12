# ExecPlan — CLIP-047 brief/content-plan engine

## Ticket and objective
CLIP-047 (M11): content briefs grounded in research packets plus a
deterministic, factuality-gated content-plan engine with append-only
versioning. No LLM, no provider identifiers.

## Current repository facts
- Head at plan time: `5cf4a40` (CLIP-046 state advance). CI green.
- CLIP-046 landed research_packets/claims/claim_sources (migration 0027);
  claims have UNVERIFIED/VERIFIED/DISPUTED veracity.
- Next free migration: **0028** (0027 → 0028).
- Patterns: client natural-id idempotent registration (CLIP-016/046),
  pure derivation functions (CLIP-036/043), versioned rows are new but
  mirror `script_versions` intent from 03_DOMAIN.
- Worktree `/srv/clipos/worktrees/CLIP-047`, branch `agent/CLIP-047`, venv
  Python 3.14.4, lease `mimo-clipos-m1-001` expires 2026-10-12T15:25Z
  (15:25Z? — lease acquired ~11:41Z, ttl 14400 → 15:41Z).

## Scope / non-scope
Scope: `content_planning.py`, migration 0028 (content_briefs,
content_plans), `tests/test_content_planning.py`, main.py wiring,
test_migrations list, development.md.
Non-scope: hooks/scripts (CLIP-048), factuality evaluations (CLIP-049),
content_ideas table (ideas intake not in ticket title), LLM-assisted plan
generation, web UI for briefs.

## Dependencies and assumptions
- Depends on CLIP-046 (DONE). Briefs require an existing research packet;
  plans require ≥1 VERIFIED claim (fail-closed 422) — the engine's core
  factuality rule.
- Platforms are generic slugs (not provider identifiers); validated 1..64
  chars, ≤16 entries.
- Identical payload re-derivation = no-op; changed claims append version.

## Files/domains expected to change
- New: `clipos_api/content_planning.py`,
  `migrations/versions/0028_content_plans.py`,
  `tests/test_content_planning.py`, `.agent/plans/CLIP-047.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects; org-scoped; no secrets; payload carries refs
  (claim ids) + brief fields, no content blobs.
- Audits: `content_briefs.registered`, `content_plans.generated`.

## Implementation steps
1. Contracts: `ContentBrief` (platform validator), `PlanRequest`,
   `ContentPlan`; pure `derive_plan_payload`.
2. SQL rows with unique indexes (org,brief) / (org,brief,version).
3. Store: register_brief (packet existence, idempotent, conflict),
   get/list; generate_plan (brief 404 → packet 404 → verified-claims gate
   → payload compare → version append + audit), get_latest_plan.
4. Routes (5): register/get/list briefs, generate/get plan; `_map_error`.
5. Migration 0028 (revises 0027, CREATE-only).
6. Wire main.py; test_migrations += 2 tables; docs section.

## Test/gate plan
- `ruff format --check`, `ruff check`, `mypy` (CI + py3.12).
- Full pytest SQLite; full pytest CI-env.
- `alembic upgrade head` PG (0027 → 0028, idempotent).
- TestClient probes: brief→plan HTTP roundtrip (research seeded over
  HTTP), idempotent re-derive, 422 fail-closed, 404s, list.
- OpenAPI export; secret_scan, architecture_guard, state_audit verify.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed; validators guard state.

## Completion evidence
Ticket, branch, files, migration 0028, gates + results, suite count,
commit SHAs, CI run id.

## Remaining risks / follow-ups
- CLIP-048 consumes plans for hooks/scripts.
- `content_ideas` intake remains unbuilt (future).
- Next free migration: 0029.
