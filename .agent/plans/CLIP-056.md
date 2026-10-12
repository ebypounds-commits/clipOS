# ExecPlan — CLIP-056 licensed stock/b-roll adapter interface

## Ticket and objective
CLIP-056 (M13): provider-neutral stock search Protocol + deterministic
Mock, results persisted through the **existing** M7
content_sources/source_items chain with license refs at candidate time.
Interface only — media download/ingestion stays with the CLIP-038
gated flow (later tickets). No LLM, no vendor identifiers, **no new
tables**.

## Current repository facts
- Head at plan time: `873342b` (CLIP-055 state advance). CI green.
- First design draft defined a duplicate `source_items` table — caught
  at collection: M7 (rights_entities.py) already owns `content_sources`
  + `source_items` (migration 0011) with creator-parent validation and
  idempotent register_item on (source_id, external_ref). Redesigned onto
  those; migration 0037 dropped (next free stays 0037).
- Creators/sources key on bare ids -> stock ids embed the org
  (`{org}:stock-provider:{alias}` / `{org}:stock-feed:{alias}`) to avoid
  cross-tenant collisions. Creator contact_ref must match the secrets-ref
  pattern `^[a-z][a-z0-9-]*://.+$` (uses `vault://stock/{alias}`).
- Worktree `/srv/clipos/worktrees/CLIP-056`, branch `agent/CLIP-056`,
  venv ready, lease `mimo-clipos-m1-001` active.

## Scope / non-scope
Scope: `stock_adapter.py`, `tests/test_stock_adapter.py`, main.py
wiring, development.md.
Non-scope: migration (none required), real stock provider SDKs, media
download/ingestion, rights_records creation from license_ref (follow-up
before real ingestion), web UI.

## Dependencies and assumptions
- Depends on CLIP-055 (DONE). Mock emits exactly 3 CC0-1.0 items;
  org-embedded source ids give per-org isolation (foreign org list ->
  source not found 404); register_item conflict message maps to 409.

## Files/domains expected to change
- New: `clipos_api/stock_adapter.py`, `tests/test_stock_adapter.py`,
  `.agent/plans/CLIP-056.md`.
- Edit: `clipos_api/main.py`, `docs/development.md`.
- NOT edited: test_migrations (reverted — no schema change).

## Security, rights, budget and external-side-effect impact
- No external side effects; audits stock_searches.completed plus the
  rights store's creator/source/item audits; license/title in item
  metadata (refs + structured values, no content blobs).

## Implementation steps
1. Contracts + Mock (sha1 determinism, limit clamp) + id helpers.
2. search_stock: ensure creator + PARTNER_FEED source (deterministic
   org-embedded ids), register items idempotently, audit per run.
3. list_stock_items (source-derived, 404 when never searched).
4. Routes (2) with `_map_error`.
5. main.py wiring; docs.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, OpenAPI probe, secret_scan, architecture_guard, state_audit
  verify. (No alembic change.)

## Rollback / recovery plan
Revert merge if needed; validators guard state; no schema to roll back.

## Completion evidence
Ticket, branch, files, gates + results, suite count, commit SHAs, CI
run id.

## Remaining risks / follow-ups
- license_ref -> rights_records linkage before any real stock
  ingestion; CLIP-057 closes M13 after this.
- Next free migration: 0037 (unchanged).
