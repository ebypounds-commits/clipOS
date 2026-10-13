# ExecPlan — CLIP-077 Anomaly detection and finance approval gates

## Ticket and objective
CLIP-077 (M18): pure z-score cost anomaly detection with persisted
checks + one-shot finance approvals with a fail-closed gate (PRD §I/§K).

## Current repository facts
- Head at plan time: `8daa077` (CLIP-076 state advance, 82/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-077`, branch `agent/CLIP-077`,
  venv ready, lease active. Next free migration: **0055** (0054 -> 0055).

## Scope / non-scope
Scope: `clipos_api/finance_controls.py`, migration 0055 (cost_anomalies
+ finance_approvals), tests, main.py wiring, test_migrations,
docs/development.md.
Non-scope: automated remediation, approval notifications, webhook
alerts.

## Implementation
1. Pure detect_cost_anomaly (population stddev, over-cost only,
   insufficient_data below 2 baseline points, zero-variance rules) +
   evaluate_approval_gate (not_approved/currency/amount denies).
2. FinanceControlStore: run_anomaly_check (entry 404, peers by
   source+category, idempotent/conflict, audited), approvals
   submit/resolve one-shot (404/409, audited), gate (audited checked),
   list/get.
3. Migration 0055; 6 routes; docs.

## Test/gate plan
~18 tests; ruff/mypy; pytest SQLite + CI-env; PG 0054->0055;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (82->83, current -> CLIP-078)
-> push -> CI green -> release lease, remove worktree, local pull.