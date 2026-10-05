# Codex Task Packet — CLIP-002

## Lease
- ticket_id: CLIP-002
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-002
- worktree: /srv/clipos/worktrees/CLIP-002
- lease_expires_at: +60 minutes from dispatch

## Objective
Create the Docker local infrastructure and health checks for M0: a non-colliding dev compose stack, a typed health contract, an idempotent health-check script, Make targets, CI integration coverage, and docs.

## Environment constraints (IMPORTANT)
- This sandbox may have NO network and NO docker socket. You CANNOT pip/npm install or run containers inside this session — that is expected. Design gates so the orchestrator can run them outside the sandbox; run only what is runnable here (file writes, python3 stdlib, docker compose config if docker responds).
- Ports on this host are OCCUPIED by operator services: 5432 (live Postgres!), 6379 maybe, 9000/9001 maybe. NEVER bind 5432, 6379, 80, 443, 3000, 3080, 5432, 8080, 8090, 18080.

## Required reading
09_MILESTONES_M0_M24.md (M0 gates), 02_SYSTEM_ARCHITECTURE.md (§5 deployment topology), 11_CI_CD_DEPLOYMENT.md, 12_OPERATIONS_RUNBOOK.md (health conventions), 07_SECURITY_GOVERNANCE_RIGHTS.md (secrets posture), AGENTS.md, docs/development.md (current state from CLIP-001).

## Deliverables (exact paths — orchestrator verifies)
1. `docker-compose.dev.yml` REVISION:
   - top-level `name: clipos-dev` (stable project name independent of directory)
   - port remap: postgres `5433:5432`, redis `6380:6379`, minio `9002:9000` + `9003:9001`
   - keep all existing healthchecks/services/profiles (api, web under `app` profile)
   - add `restart: "no"` where sensible; keep dev-only credentials clearly non-production
2. `services/api/clipos_api/infrastructure.py` — typed contract: pydantic models `ServiceHealth(name: str, status: Literal["healthy","unhealthy","starting","absent"], detail: str|None, checked_at: datetime)` and `InfrastructureHealth(services: list[ServiceHealth], overall: Literal["healthy","degraded"])` with `overall` computed; plus a parser `parse_compose_json(raw: str) -> InfrastructureHealth` that converts `docker compose ps --format json` output (one JSON object per line) into the model, mapping health status (`healthy`→healthy, `unhealthy`→unhealthy, `starting`→starting, running without healthcheck→starting, missing→absent).
3. `services/api/tests/test_infrastructure.py` — unit tests for the parser (sample compose ps lines incl. unhealthy/starting/absent/multiple services, overall degraded logic, schema round-trip). Must run WITHOUT docker.
4. `scripts/infrastructure_health.sh` — bash, `set -euo pipefail`:
   - `cd` to repo root (script dir/..)
   - runs `docker compose -f docker-compose.dev.yml ps --format json` (project name from file)
   - pipes through a small inline python (use `services/api/clipos_api/infrastructure.py` via `PYTHONPATH=services/api`, falling back to stdlib-only if import fails) to print a JSON InfrastructureHealth summary
   - exit 0 if overall healthy (postgres+redis+minio all healthy), else 1
   - idempotent/read-only: never starts, stops, or mutates containers; safe to run repeatedly
5. `Makefile` — add targets: `infra-up` (`docker compose -f docker-compose.dev.yml up -d --wait || docker compose -f docker-compose.dev.yml up -d` then health script), `infra-health` (runs scripts/infrastructure_health.sh), `infra-down` (`docker compose -f docker-compose.dev.yml down`), keep existing targets working.
6. `.github/workflows/baseline-ci.yml` — extend test job (or add `integration` job needing test): after compose config step add:
   - `docker compose -f docker-compose.dev.yml up -d`
   - wait loop ≤90s polling `bash scripts/infrastructure_health.sh` until exit 0 (print compose ps output on failure)
   - `bash scripts/infrastructure_health.sh`
   - always-run cleanup step `docker compose -f docker-compose.dev.yml down -v` (`if: always()`)
   Keep validate/report jobs correct: report still reflects validate+test (and integration if it becomes a job).
7. `docs/development.md` — infra section: how to bring stack up, health-check usage, port map (5433/6380/9002/9003), why 5432 is avoided (operator Postgres).
8. `.agent/dryrun/CLIP-002_completion_report.json` per schema.

## Acceptance (orchestrator re-runs)
- pytest: existing 8 + new test_infrastructure tests all green (outside sandbox).
- `docker compose -f docker-compose.dev.yml config -q` exit 0; NO host port 5432/6379 in the file (grep gate).
- Live stack test (orchestrator): `up -d` → `scripts/infrastructure_health.sh` exit 0 → `down` — and a SECOND `up`/`down` cycle works (idempotence).
- ruff clean; validate_handoff OK; secret_scan clean; tsc unchanged (web untouched) but CI stays green.
- 10_TICKETS.yaml/BUILD_STATE.json/docs/reports/* untouched.

## Allowed scope
paths: docker-compose.dev.yml, scripts/infrastructure_health.sh, services/api/clipos_api/infrastructure.py, services/api/tests/**, Makefile, .github/workflows/baseline-ci.yml, docs/development.md, .agent/**
network actions: none (no pip/npm installs in-session; no pushes)
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json; binding operator ports; weakening existing CI jobs; fabricating test results you did not run (report SKIP with reason instead).

## Required gates (run what is runnable here; report SKIP+reason for the rest)
- python3 -m pytest -q services/api (if pytest importable, else SKIP)
- python3 -m compileall services/api/clipos_api
- docker compose -f docker-compose.dev.yml config -q (if docker responds, else SKIP)
- python3 scripts/validate_handoff.py ; bash scripts/secret_scan.sh

## Completion output
Write the completion report (status BLOCKED only for genuinely sandbox-blocked gates; include which), print a concise summary. Do not fabricate.
