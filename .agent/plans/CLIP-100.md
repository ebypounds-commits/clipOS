# CLIP-100 — SLOs / cost caps / kill switches / incident drills (M24)

## Goal

M24's exit gate pairs "SLOs" with end-to-end production qualification, guarded
public mode, the operator runbook and autonomy evidence. Before this ticket:

- **SLOs had no implementation anywhere.** The only occurrence of the term
  was the M24 sentence in `09_MILESTONES_M0_M24.md`.
- The other operational controls existed in separate modules — `budgets`
  (cost caps), `publishing` and `remediation` (kill switches), `incidents`
  (lifecycle + runbooks), `monitors`, `backup_verification` (restore drills)
  — but nothing asserted that they *work* or that anyone exercised them.

This ticket adds the SLO concept and one operator-visible verdict saying
whether the operational controls have been demonstrated.

## Deliverables

| Path | Purpose |
| --- | --- |
| `services/api/clipos_api/slo_qualification.py` | Objectives, pure SLO/cap math, drill records, verdict, routes |
| `services/api/migrations/versions/0073_slo_qualification.py` | `slo_objectives`, `ops_drills` |
| `services/api/tests/test_slo_qualification.py` | 34 tests |
| `main.py` / `test_migrations.py` | Router wiring + expected table list |

## The three design decisions worth reviewing

**1. `INSUFFICIENT_DATA` is its own status, not a pass.**
`evaluate_slo` returns `MET`, `BREACHED` or `INSUFFICIENT_DATA`. An objective
with fewer observations than its `min_samples` cannot be judged, and
reporting "we did not measure enough" as healthy is exactly the dishonesty
this gate exists to prevent. It fails the aggregate verdict, and the finding
reads `SLO unmeasured` rather than `SLO met`.

**2. `verify_cost_cap` checks that the cap refuses, in both directions.**
It takes the budgets authorizer as a parameter (no cross-module import, and
stub-friendly). An authorizer that allows over-cap spend is broken; so is
one that refuses a within-cap request, because that would stall legitimate
work.

**3. Untested controls are reported as unverified.**
`qualify_operations` has `REQUIRED_CONTROLS = (kill_switch_closed,
cost_cap_refused, incident_drill)`. A control with no `VERIFIED` drill
appears in `controls_unverified` and fails the verdict — "nobody drilled it"
never renders as "it works". `PASS` requires every control exercised.

## Cost-cap integration

`verify_cost_cap(authorize_spend, cap, spent, requested)` consumes the real
`budgets.authorize_spend`, whose `SpendDecision` carries `.allowed` and
`.reason`. The argument is passed in, so importing `slo_qualification` never
drags in `budgets`.

## Operational note: root-owned fixture files

Two failures in this ticket were environment artefacts, not code defects:

- Postgres was down (compose stack stopped); restarted with
  `docker compose -f docker-compose.dev.yml up -d`.
- Running pytest **as root** created `/tmp/clipos-100-slo-audit.log`
  root-owned, so the next clipos-user run hit `PermissionError` writing the
  fixture. Removed. Always run the suite as `clipos`.

## Verification

- 34 new tests; 1799 passed CI-env (94.02% cov), 1 skipped.
- ruff format + check clean (302 files); mypy clean (111 files).
- Migration 0073 applied on Postgres (0072 -> 0073).
- secret_scan clean; architecture_guard 6/6; deploy_topology_check PASS;
  state_audit 105/105.
