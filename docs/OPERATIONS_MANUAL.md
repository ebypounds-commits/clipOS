# ClipOS Operations Manual

This is the consolidated operator manual for running ClipOS. It maps every
operator-facing task to the route, script, or document that implements it.
The source documents it consolidates remain authoritative for their topics:
`12_OPERATIONS_RUNBOOK.md` (policy baseline), `11_CI_CD_DEPLOYMENT.md`
(deployment/rollback policy), `13_HUMAN_GATES_AND_SECRETS.md` (gates),
`docs/PRODUCTION_BOOTSTRAP.md` (bring-up procedure), `deploy/README.md`
(topologies), and `docs/development.md` (module-level contracts).

## 1. Health and status

| What | Where |
| --- | --- |
| API liveness/version | `GET /health` |
| Operator health (no secret leakage) | `GET /operator/health` |
| Compose infrastructure health | `bash scripts/infrastructure_health.sh` |
| Resource monitors (credentials, quota, disk, provider) | `POST /monitors/check`, `GET /monitors` |
| SLO posture and qualification | `GET /slo/qualification`, `POST /slo/objectives/{id}/evaluate` |
| Security controls against a live instance | `python3 scripts/security_qualification.py` |
| End-to-end private-mode pipeline | `python3 scripts/private_mode_qualification.py` |
| Autonomy evidence | `python3 scripts/autonomy_evidence.py` |
| Schema agreement (models vs migrations) | `DATABASE_URL=... python3 scripts/schema_drift.py` |

The qualification harnesses are the fastest way to answer "is the system
actually working": each emits a versioned JSON report and exits non-zero on
failure, so a broken environment is never mistaken for a pass.

## 2. Deployment lifecycle

Topologies, hardening invariants, and bring-up commands live in
`deploy/README.md`. The short version:

1. **Bootstrap** — follow `docs/PRODUCTION_BOOTSTRAP.md`: declare a bootstrap
   manifest (`POST /production/bootstrap`), validate it, clear the findings,
   then execute the two human gates (secret import, TLS issuance).
2. **Migrate** — `alembic upgrade head` before the new image starts.
3. **Canary** — `POST /deployments` (strategy `CANARY`), then
   `POST /deployments/{id}/promote` once the health window passes. Promotion
   is refused until a non-empty all-HEALTHY window is recorded.
4. **Roll back** — `POST /deployments/{id}/rollback` with a target revision.
   Rollback refuses any target whose migrations the current release applied
   (expand/contract discipline). Restore-from-backup is disaster recovery,
   not rollback.

Release evidence is recorded per deployment via
`POST /deployments/{id}/evidence` and carries the exact fields the
deployment specification requires.

## 3. Publishing governance

Publishing is governed by per-(org, platform) policy plus an owner gate:

- Policy: `POST /publish-policies` — allowed visibilities, `public_enabled`
  (default false), `kill_switch`.
- Operating modes: OBSERVE and DRAFT deny all publishing; PRIVATE_TEST allows
  only PRIVATE/UNLISTED; PUBLIC additionally requires `public_enabled` **and**
  a live owner approval for that exact account (`POST
  /publishing/owner-approvals`, after a recorded passing private test via
  `POST /publishing/private-tests`).
- The gate is per real platform account: approving one channel cannot unlock
  another. `GET /publishing/gate-summary` shows, per account, whether public
  publishing is allowed and why not.
- Kill switch: set `kill_switch: true` on the policy — it denies every
  submission for that platform regardless of mode or visibility.

**First public posting on each real account is a human gate.** The guard is
built and tested (Mock adapter only); the owner executes the first post.

## 4. Incidents, remediation, drills

- Incidents: `POST /incidents` with severity and optional runbook binding;
  transitions follow the declared lifecycle (`GET /incidents/{id}/lifecycle`
  history via `/agents`-style routes on incidents module); every move is
  audited.
- Runbooks: `POST /runbooks` with ordered steps; an incident may bind a
  runbook whose category must match.
- Auto-remediation: allow-listed actions only (`POST /remediation-actions`),
  every run gated (`kill_switch_active`, `not_allow_listed`, `params_invalid`,
  `rate_limited`, `approval_required`); denied runs are recorded and the
  executor never invoked.
- Drills: record exercised controls via `POST /slo/drills`. The operational
  qualification fails while any of `kill_switch_closed`, `cost_cap_refused`,
  `incident_drill` has no VERIFIED drill — an untested control is reported as
  unverified, never as working.

## 5. Cost control

- Budgets cap spend per scope over an explicit window
  (`POST /budgets`); authorization is fail-closed
  (`POST /budgets/{id}/authorize`) and every decision is persisted with its
  reason.
- SLO objectives (`POST /slo/objectives`) declare targets in basis points;
  evaluation distinguishes MET, BREACHED and INSUFFICIENT_DATA — insufficient
  data is never rendered as healthy.

## 6. Backups

- Manifests: `python3 scripts/backup.py create|verify|pgdump`.
- Encrypted artifacts and restore tests: `POST /backups/artifacts`,
  `POST /backups/restore-tests`, `GET /backups/status`.
- Restore verification keeps wrong-bytes and over-RTO as distinct failure
  modes; backup freshness follows the daily cadence (HEALTHY/DEGRADED/DOWN).

## 7. Secrets and rotation

Names only ever live in the repository
(`deploy/env.*.example`, `BUILD_STATE` gate declarations). Real values enter
through the server secret manager at execution time. Every connector must
have a registered rotation procedure
(`POST /production/rotations`); a bootstrap validation asked to check a
connector list fails while any connector lacks one. See
`docs/PRODUCTION_BOOTSTRAP.md` for the full inventory.

## 8. Human gates

All ten gates in `13_HUMAN_GATES_AND_SECRETS.md` are owner acts. The build
records their state in `BUILD_STATE.human_gates`; the autonomy evidence
report fails if public publishing is ever enabled while the
`first_public_post` gate is still closed. Gates are facts, not faults: an
open gate is reported as open, not penalised.

## 9. Verification commands

```bash
python3 scripts/autonomy_evidence.py          # the build's claims vs each other
python3 scripts/deploy_topology_check.py      # compose hardening H1..H8
python3 scripts/schema_drift.py               # models vs migrations
python3 scripts/security_qualification.py     # M22 controls, live
python3 scripts/private_mode_qualification.py # E2E private pipeline, live
bash scripts/secret_scan.sh                   # no secret-shaped literals
python3 scripts/state_audit.py verify         # state transitions covered
python3 scripts/validate_state.py             # BUILD_STATE well-formed
```

## 10. What the automation does not do

It does not import production secrets, issue TLS certificates, connect real
platform accounts, make the first public post, or approve high-risk agent
activation. Those are the human gates. Everything above exists so that when
an operator reaches one of them, the system's claims have already been
checked against each other — and so that refusing to claim "healthy" without
evidence is enforced by code rather than by hope.
