# ExecPlan — CLIP-071 Affiliate link/click/conversion attribution

## Ticket and objective
CLIP-071 (M17): affiliate links, click/conversion event records with
optional click attribution, and a pure per-link attribution summary
(counts, conversion rate, revenue per currency) per PRD §I.

## Current repository facts
- Head at plan time: `0bae378` (CLIP-070 state advance, 76/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-071`, branch `agent/CLIP-071`,
  venv ready, lease active. Next free migration: **0049** (0048 -> 0049).

## Scope / non-scope
Scope: `clipos_api/affiliate_attribution.py`, migration 0049 (3
tables), tests, main.py wiring, test_migrations, docs/development.md.
Non-scope: click-tracking redirect service, fraud detection, cookie
management, frontend widgets.

## Implementation
1. affiliate_links (unique org+link), affiliate_clicks (unique
   org+click, link FK-style 404), affiliate_conversions (unique
   org+conversion, integer amount_cents, optional click_ref validated
   to belong to same link).
2. summarize_attribution pure: rate = conversions/clicks (0 without
   clicks), revenue summed per currency (never mixed).
3. Routes: POST/GET /affiliate-links, GET /{link_id}, GET
   /{link_id}/attribution, POST/GET /affiliate-clicks, POST/GET
   /affiliate-conversions. Audits registered/recorded.

## Test/gate plan
~18 tests; ruff/mypy; pytest SQLite + CI-env; PG 0048->0049;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (76->77, current -> CLIP-072)
-> push -> CI green -> release lease, remove worktree, local pull.