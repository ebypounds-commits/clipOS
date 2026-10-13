# ExecPlan — CLIP-078 Brand/channel templates

## Ticket and objective
CLIP-078 (M19 opener): reusable brand templates (voice/color/logo
refs) and per-channel posting templates with brand linkage (PRD brand
kit).

## Current repository facts
- Head at plan time: `1733d96` (CLIP-077 state advance, 83/102, M18
  closed, M19 current). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-078`, branch `agent/CLIP-078`,
  venv ready, lease active. Next free migration: **0056** (0055 -> 0056).

## Scope / non-scope
Scope: `clipos_api/brand_templates.py`, migration 0056 (brand_templates
+ channel_templates), tests, main.py wiring, test_migrations,
docs/development.md.
Non-scope: template rendering/application to renders, media asset
upload, frontend editors.

## Implementation
1. Contracts with validators (hex palette, hashtag pattern) — bad
   input 422 at pydantic layer; Visibility reused from CLIP-058.
2. BrandTemplateStore: brand register idempotent/conflict/audited,
   channel register validates brand link (404) idempotent/conflict,
   get/list with platform filter.
3. Migration 0056; 6 routes; docs.

## Test/gate plan
~16 tests; ruff/mypy; pytest SQLite + CI-env; PG 0055->0056;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (83->84, current -> CLIP-079)
-> push -> CI green -> release lease, remove worktree, local pull.