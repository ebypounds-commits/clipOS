# ExecPlan — CLIP-059 YouTube publishing/status adapter

## Ticket and objective
CLIP-059 (M14): YouTube adapter implementing the CLIP-058
PublishingAdapter Protocol as a deterministic Mock + platform account
registry for channel/OAuth-scope storage (08_INTEGRATIONS YouTube
section). Real Data API integration replaces mock later; unavailable
real credentials do not block implementation.

## Current repository facts
- Head at plan time: `7c17808` (CLIP-058 state advance). CI green.
- CLIP-058: PublishingAdapter Protocol, publish_policies, publication_jobs,
  ADAPTERS registry seam (register_adapter/get_adapter) + MockPublishingAdapter
  fallback. Worktree `/srv/clipos/worktrees/CLIP-059`, branch
  `agent/CLIP-059`, venv ready, lease `mimo-clipos-m1-001` active.
- Next free migration: **0039** (0038 -> 0039).

## Scope / non-scope
Scope: `platform_accounts.py`, `youtube_adapter.py`, migration 0039,
`tests/test_youtube_adapter.py`, main.py wiring (import youtube_adapter
for registration), test_migrations, docs, development.md.
Non-scope: real YouTube Data API SDK integration, resumable upload
infrastructure, OAuth token management, TikTok/Meta adapters
(CLIP-060/061), web UI for account management, production publishing.

## Dependencies and assumptions
- Depends on CLIP-058 (DONE). adapter registry seam `register_adapter`
  already exists in publishing.py. YouTube mock registers at module
  import time; get_adapter("youtube") returns it.

## Files/domains expected to change
- New: `clipos_api/platform_accounts.py`, `clipos_api/youtube_adapter.py`,
  `migrations/versions/0039_platform_accounts.py`,
  `tests/test_platform_accounts.py` (not yet written, CLIP-059),
  `tests/test_youtube_adapter.py`, `.agent/plans/CLIP-059.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects; vault references only (no OAuth tokens stored).
  Audits: platform_accounts.registered, youtube_adapter.registered.

## Implementation steps
1. platform_accounts: table + store (idempotent (org, platform, ref),
   conflict) + routes (register/get/list with platform filter).
2. youtube_adapter: YouTubePublishingAdapter mock + register_adapter call.
3. main.py: import youtube_adapter (registration at load) + platform_accounts
   router; test_migrations += platform_accounts.
4. Migration 0039; docs section.
5. Tests: platform_accounts store + routes, adapter health/publish/status,
   adapter registration, publishing route YouTube adapter usage, fallback
   for unknown platform.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG 0038 -> 0039, OpenAPI probe, secret_scan,
  architecture_guard, state_audit verify.
- M14 opens the series; same pattern repeats for TikTok (CLIP-060).

## Rollback / recovery plan
CREATE-only migration; revert merge if needed; validators guard state.

## Completion evidence
Ticket, branch, files, migration 0039, gates + results, suite count,
commit SHAs, CI run id.

## Remaining risks / follow-ups
- CLIP-060 (TikTok), CLIP-061 (Meta), CLIP-062 (publishing status
  reconciliation) remain in M14.
- Real YouTube integration requires OAuth + Data API SDK — documented
  in 08 spec.
- Next free migration: 0040.