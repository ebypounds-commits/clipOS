# ExecPlan — CLIP-090 Threat model and abuse cases

## Ticket and objective
CLIP-090 (M22 opener): machine-readable threat model — threats with
STRIDE categories and abuse-case narratives, mitigation-to-control
mappings that must point at real code, abuse cases with attack
surfaces, and a pure coverage audit that exposes unbacked security
claims (grounded in 07_SECURITY_GOVERNANCE_RIGHTS.md).

## Current repository facts
- Head at plan time: `2a6e654` (CLIP-089 state advance, 95/102, M21
  closed, M22 current). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-090`, branch `agent/CLIP-090`,
  venv ready, lease active. Next free migration: **0067** (0066 -> 0067).

## Scope / non-scope
Scope: `clipos_api/threat_model.py`, migration 0067 (threats,
threat_controls, threat_mitigations, abuse_cases), tests, main.py
wiring, test_migrations, docs/development.md.
Non-scope: automated exploit testing (CLIP-092), hardening changes
(CLIP-091), security qualification run (CLIP-093).

## Implementation
1. Contracts: STRIDE category, severity, review status with pure
   transition table; threat + mitigation + abuse case models.
2. `_module_exists`/`audit_threat_coverage`: pure coverage verdicts
   covered / unevidenced / uncovered using importlib spec resolution so
   a claim must point at real code.
3. ThreatStore: register threat (+ controls), mitigation (404/409),
   transition (audited), abuse case (404/409), coverage.
4. Migration 0067; 8 routes; docs.

## Test/gate plan
~18 tests; ruff/mypy; pytest SQLite + CI-env; PG 0066->0067;
OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (95->96, current -> CLIP-091)
-> push -> CI green -> release lease, remove worktree, local pull.