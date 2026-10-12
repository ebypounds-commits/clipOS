# ExecPlan — CLIP-061 Meta publishing/status adapter

## Ticket and objective
CLIP-061 (M14): Meta adapter implementing the CLIP-058
PublishingAdapter Protocol as a deterministic Mock. Real Graph API
integration replaces mock later; unavailable real credentials and
unreachable developer docs (HTTP 400) do not block implementation. No
new migration — platform_accounts table (0039) already covers Meta
accounts.

## Current repository facts
- Head at plan time: `519004a` (CLIP-060 ruff fix). CI green.
- CLIP-058: PublishingAdapter Protocol, publish_policies, publication_jobs,
  ADAPTERS registry seam + MockPublishingAdapter fallback.
- CLIP-059: YouTubePublishingAdapter mock registered at import.
- CLIP-060: TikTokPublishingAdapter mock registered at import.
- Worktree `/srv/clipos/worktrees/CLIP-061`, branch `agent/CLIP-061`,
  venv ready.

## Scope / non-scope
Scope: `clipos_api/meta_adapter.py`, `tests/test_meta_adapter.py`,
main.py wiring (import meta_adapter for registration), docs/development.md.
Non-scope: real Meta Graph API SDK integration, OAuth token management,
new migration, new routes.

## Dependencies and assumptions
- Depends on CLIP-060 (DONE). adapter registry seam `register_adapter`
  already exists in publishing.py. platform_accounts table already has
  meta as a valid platform. No migration change needed.

## Files/domains expected to change
- New: `clipos_api/meta_adapter.py`, `tests/test_meta_adapter.py`,
  `.agent/plans/CLIP-061.md`.
- Edit: `clipos_api/main.py` (add `import clipos_api.meta_adapter`),
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects; mock only. Audits: meta_adapter.registered.
- Meta developer docs returned HTTP 400 at time of implementation;
  real adapter needs current-doc verification when credentials provisioned.

## Implementation steps
1. meta_adapter.py: MetaPublishingAdapter mock (health_check returns
   ok with ["upload", "page_publish", "status", "privacy"], publish derives
   meta-mock-{sha1(key)[:10]}, get_status SUCCEEDED) + register_adapter("meta")
   at import.
2. test_meta_adapter.py: adapter registration, health, publish determinism,
   platform isolation (meta vs youtube vs tiktok), submit_publication with meta
   adapter, route roundtrip (policy + publish + fetch), DENIED returns 200.
3. main.py: add `import clipos_api.meta_adapter` after tiktok_adapter line.
4. development.md: Meta adapter section.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG head (no new migration, verify no-op), OpenAPI probe,
  secret_scan, architecture_guard, state_audit verify.

## Rollback / recovery plan
No migration; revert merge if needed; validators guard state.

## Completion evidence
Ticket, branch, files, gates + results, suite count, commit SHAs, CI run id.

## Remaining risks / follow-ups
- Real Meta integration requires OAuth + Graph API SDK — documented
  in 08 spec.
- No new migration needed (0039 platform_accounts already covers meta).
- M14 complete after CLIP-061: all three platform adapters done.