# Codex Task Packet — CLIP-014

## Lease
- ticket_id: CLIP-014
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-014
- worktree: /srv/clipos/worktrees/CLIP-014
- lease_expires_at: +60 minutes from dispatch

## Objective
Deploy Temporal dev/self-host configuration (first M3 ticket): (a) add `temporal` + `temporal-ui` services to `docker-compose.dev.yml` — pinned REAL images `temporalio/auto-setup:1.29.7` (verified on Docker Hub; DB=postgres12 against the compose postgres, separate `temporal` database, port 7233) and `temporalio/ui:2.55.0` (TEMPORAL_ADDRESS=temporal:7233, host port 8233 → container 8088); (b) extend `scripts/infrastructure_health.sh` (+ compose healthchecks) so Temporal is part of the health gate the test job already runs; (c) typed `TemporalConfig.from_env()` in `services/api/clipos_api/temporal_config.py` (address, namespace default `clipos-dev`, ui_url, connect_timeout; env-only, validation errors on bad values) + a `probe_temporal(config)` connectivity helper using `temporalio.Client.connect` (timeout-bounded, no credentials stored); (d) wire `TEMPORAL_ADDRESS` env into the CI test job (compose already starts it there) so the SDK-gated integration test runs in CI; (e) docs.

## Environment constraints
- Sandbox: NO network, NO docker. **venv EXISTS with temporalio 1.34.0 pre-installed** — self-verify: `.venv/bin/pytest -q services/api`, `.venv/bin/ruff format services/api`, `.venv/bin/ruff check --fix services/api`, `.venv/bin/ruff check services/api`, `.venv/bin/mypy services/api/clipos_api`. `probe_temporal` integration test gates on `TEMPORAL_ADDRESS` (SKIPs in-sandbox; orchestrator + CI run it live). `docker compose config -q` unavailable in-sandbox — validate YAML by python yaml.safe_load; orchestrator runs the live compose.
- NO new deps beyond temporalio (already in venv — ADD `temporalio>=1.34.*` floor to pyproject from the installed version).
- Image tags are PINNED above — do not change them; ports 7233/8233 verified free; NEVER touch operator ports (22 53 80 443 3000 3080 3100 5432 6001 6002 8000 8080 8090 8642 8776 8888 9119 9377 11235 18080) — dev compose uses 5433/6380/9002/9003 today.

## Required reading
02_SYSTEM_ARCHITECTURE.md (Durable orchestration: self-hosted Temporal; topology: temporal + temporal-ui containers; initial single-server list), 09_MILESTONES_M0_M24.md (M3 Temporal Orchestration Foundation), 06_WORKFLOW_ORCHESTRATION.md (design principles — config only, NO workflow code this ticket), 12_OPERATIONS_RUNBOOK.md (daily checks verify Temporal health), docker-compose.dev.yml, scripts/infrastructure_health.sh, services/api/clipos_api/infrastructure.py (ServiceHealth patterns), .github/workflows/baseline-ci.yml (test job compose+health), docs/development.md, .agent/RUNNER_CONVENTIONS.md.

## Scope boundaries
- Config/deploy ONLY: no workflows, activities, workers, schedules, or replay code (CLIP-015/016/017 territory); no Temporal Cloud; no auth/mTLS (dev self-host, document as roadmap).
- Do NOT modify CLIP-006..013 modules; temporal database is CREATED by auto-setup (never hand-create it); no new listener ports beyond 7233/8233.
- infrastructure_health.sh changes must keep existing pg/redis/minio checks intact and continue honoring its exit-code contract (the CI test job gates on it, polled up to 90s).

## Deliverables (exact paths — orchestrator verifies)
1. `docker-compose.dev.yml` — temporal + temporal-ui services (pinned images above; postgres12 DB config reusing compose postgres with `temporal` database and the EXISTING superuser env vars pattern from the file; depends_on postgres healthy; healthchecks on both).
2. `scripts/infrastructure_health.sh` — temporal section: (i) temporal container health/running, (ii) gRPC port 7233 reachable inside the compose network (use a lightweight check available in the container/network — e.g. `docker compose exec` TCP/CLI probe, no new host deps), (iii) UI HTTP responds on host port 8233 (curl/wget, short timeout). Keep it idempotent, read-only, fast (<5s when healthy).
3. `services/api/clipos_api/temporal_config.py` — frozen `TemporalConfig` (address, namespace, ui_url, connect_timeout_seconds; `from_env()` reading TEMPORAL_ADDRESS/TEMPORAL_NAMESPACE/TEMPORAL_UI_URL/TEMPORAL_CONNECT_TIMEOUT with defaults address `localhost:7233`, namespace `clipos-dev`, ui `http://localhost:8233`, timeout 5; validation error on empty/malformed address or non-positive timeout) + `async def probe_temporal(config) -> TemporalProbeResult` (frozen: ok: bool, detail: str; `temporalio.Client.connect(address, namespace=...)` within timeout; NEVER log/return credentials — none exist; failures return ok=False with sanitized detail, exceptions never propagate raw connection strings).
4. `services/api/pyproject.toml` — temporalio floor from installed version (e.g. `temporalio>=1.34`).
5. `services/api/tests/test_temporal_config.py` — unit: defaults; env override; validation errors (empty address, bad timeout); probe result frozen; probe with unreachable address (127.0.0.1:1, timeout 1) returns ok=False sanitized detail WITHOUT raising and WITHOUT the address string... (detail MAY contain the host:port — that is not a secret — but must not contain traceback internals; assert no 'Traceback' in detail). Plus `@pytest.mark.skipif(not os.environ.get("TEMPORAL_ADDRESS"))` live test: probe against running compose temporal returns ok=True (this runs in CI where TEMPORAL_ADDRESS is set, and on the VPS orchestrator run).
6. Existing infrastructure tests: if any assert the exact compose service set, update them HONESTLY to include temporal/ui (evidence in report); do not weaken assertions.
7. `.github/workflows/baseline-ci.yml` — add `TEMPORAL_ADDRESS: localhost:7233` to the test job env alongside DATABASE_URL/STORAGE_*/CACHE_REDIS_URL; nothing else changes; YAML must parse; report still needs all four jobs.
8. `docs/development.md` — "Temporal (dev/self-host)" paragraph: services/ports, health integration, TemporalConfig usage, probe example, roadmap note (workers in CLIP-015, auth/mTLS later).
9. `.agent/dryrun/CLIP-014_completion_report.json` with REAL gate totals.

## Acceptance (orchestrator re-runs outside sandbox — incl. LIVE Temporal)
- Sandbox: pytest green (live probe SKIPs), ruff/mypy clean, yaml parses.
- Orchestrator: `docker compose config -q` valid; `up -d` → `infrastructure_health.sh` green with temporal; LIVE `probe_temporal` via python (TEMPORAL_ADDRESS=localhost:7233) returns ok=True; UI HTTP 8233 responds; existing pg/redis/minio checks still pass; teardown `down -v`.
- CI 4 jobs green after merge; no secrets; no provider identifiers where aliases apply; existing tests untouched-or-honestly-updated.

## Allowed scope
paths: docker-compose.dev.yml, scripts/infrastructure_health.sh, services/api/{pyproject.toml,clipos_api/temporal_config.py,tests/test_temporal_config.py,tests/test_infrastructure.py*}, .github/workflows/baseline-ci.yml, docs/development.md, .agent/**
network: none in-session (images pulled by orchestrator/compose)
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; workflow/activity/worker code; Temporal Cloud or auth claims; weakening health checks or tests; fabricating results.

## Required gates (runnable here — RUN THEM)
- .venv/bin/pytest -q services/api (green; live temporal test SKIP)
- .venv/bin/ruff format services/api && .venv/bin/ruff check --fix services/api && .venv/bin/ruff check services/api && .venv/bin/ruff format --check services/api
- .venv/bin/mypy services/api/clipos_api
- python3 -m compileall services/api scripts
- python3 scripts/validate_handoff.py; python3 scripts/validate_state.py; python3 scripts/state_audit.py verify; python3 scripts/architecture_guard.py; python3 scripts/validate_packet.py .agent/task_packet_CLIP-014.md
- bash scripts/secret_scan.sh (RUN `git add -A` FIRST — scan reads tracked files only); yaml parse of baseline-ci.yml AND docker-compose.dev.yml

## Completion output
Completion report + concise summary; no fabrication.
