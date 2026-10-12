# ExecPlan — CLIP-060 TikTok publishing/status adapter

## Ticket and objective
CLIP-060 (M14): TikTok adapter implementing the CLIP-058
PublishingAdapter Protocol as a deterministic Mock. Real Content Posting
API integration replaces mock later; unavailable real credentials do not
block implementation. No new migration — platform_accounts table (0039)
already covers TikTok accounts.

## Current repository facts
- Head at plan time: `612393a` (CLIP-059 state advance). CI green.
- CLIP-058: PublishingAdapter Protocol, publish_policies, publication_jobs,
  ADAPTERS registry seam + MockPublishingAdapter fallback.
- CLIP-059: YouTubePublishingAdapter mock registered at import, platform_accounts
  table (migration 0039), publishing route uses get_adapter(platform).
- Worktree `/srv/clipos/worktrees/CLIP-060`, branch `agent/CLIP-060`,
  venv ready.

## Scope / non-scope
Scope: `clipos_api/tiktok_adapter.py`, `tests/test_tiktok_adapter.py`,
main.py wiring (import tiktok_adapter for registration), docs/development.md.
Non-scope: real TikTok Content Posting API SDK integration, OAuth token
management, Meta adapter (CLIP-061), new migration, new routes.

## Dependencies and assumptions
- Depends on CLIP-059 (DONE). adapter registry seam `register_adapter`
  already exists in publishing.py. platform_accounts table already has
  tiktok as a valid platform. No migration change needed.

## Files/domains expected to change
- New: `clipos_api/tiktok_adapter.py`, `tests/test_tiktok_adapter.py`,
  `.agent/plans/CLIP-060.md`.
- Edit: `clipos_api/main.py` (add `import clipos_api.tiktok_adapter`),
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects; mock only. Audits: tiktok_adapter.registered.

## Implementation steps
1. tiktok_adapter.py: TikTokPublishingAdapter mock (health_check returns
   ok with ["upload", "privacy", "status", "creator_info"], publish derives
   tt-mock-{sha1(key)[:10]}, get_status SUCCEEDED) + register_adapter("tiktok")
   at import.
2. test_tiktok_adapter.py: adapter registration, health, publish determinism,
   platform isolation (tiktok vs youtube), submit_publication with tiktok
   adapter, route roundtrip (policy + publish + fetch), DENIED returns 200.
3. main.py: add `import clipos_api.tiktok_adapter` after youtube_adapter line.
4. development.md: TikTok adapter section.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG head (no new migration, verify no-op), OpenAPI probe,
  secret_scan, architecture_guard, state_audit verify.

## Rollback / recovery plan
No migration; revert merge if needed; validators guard state.

## Completion evidence
Ticket, branch, files, gates + results, suite count, commit SHAs, CI run id.

## Remaining risks / follow-ups
- CLIP-061 (Meta adapter) is the next M14 ticket.
- Real TikTok integration requires OAuth + Content Posting API SDK —
  documented in 08 spec.
- No new migration needed (0039 platform_accounts already covers tiktok).