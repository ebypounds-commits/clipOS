# ExecPlan — CLIP-072 Sponsorship campaign attribution

## Ticket and objective
CLIP-072 (M17): sponsor campaigns with deliverables and attributed
revenue + pure campaign summary (PRD §I sponsor campaigns).

## Current repository facts
- Head at plan time: `cbc80bc` (CLIP-071 state advance, 77/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-072`, branch `agent/CLIP-072`,
  venv ready, lease active. Next free migration: **0050** (0049 -> 0050).

## Scope / non-scope
Scope: `clipos_api/sponsorship.py`, migration 0050 (3 tables), tests,
main.py wiring, test_migrations, docs/development.md.
Non-scope: contract document storage, payment processing, sponsor
portal UI.

## Implementation
1. sponsor_campaigns (unique org+campaign, integer contracted_cents,
   date window validation), sponsor_deliverables (campaign 404,
   PENDING->DELIVERED one-shot audited), sponsor_attributions
   (campaign 404, integer amount_cents, path/body campaign match 422).
2. summarize_sponsorship pure: delivery rate, revenue per currency,
   variance vs contracted when same currency.
3. Routes (8): campaigns POST/GET/summary/attributions, deliverables
   POST/GET/transition. Audits registered/delivered/recorded.

## Test/gate plan
~18 tests; ruff/mypy; pytest SQLite + CI-env; PG 0049->0050;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (77->78, current -> CLIP-073)
-> push -> CI green -> release lease, remove worktree, local pull.