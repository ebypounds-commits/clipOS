# Codex Task Packet — CLIP-013

## Lease
- ticket_id: CLIP-013
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-013
- worktree: /srv/clipos/worktrees/CLIP-013
- lease_expires_at: +60 minutes from dispatch

## Objective
Implement OpenTelemetry metrics/logging/tracing primitives and backup primitives (M2, closing the M2 foundation batch): (a) `telemetry.py` — idempotent `init_telemetry()` wiring TracerProvider/MeterProvider/OTel logging with env config (service name, mode: none|console; NO remote collector exists yet per 02's future Prometheus/Grafana/Loki — document), plus a **secret-redacting log filter** (07/13: "redact tokens in logs/traces" — masks Bearer tokens, sk-/AKIA-style keys, passwords in URLs); (b) `backup.py` + `scripts/backup.py` — backup MANIFEST primitives per 12's baseline (create: hash files into a manifest; verify: re-hash detects tampering; optional `pg_dump` logical backup gated on DATABASE_URL), with `backup freshness`-friendly structured output.

## Environment constraints
- Sandbox: NO network, NO docker. **venv EXISTS with opentelemetry-api/sdk pre-installed** — self-verify: `.venv/bin/pytest -q services/api`, `.venv/bin/ruff format services/api`, `.venv/bin/ruff check --fix services/api`, `.venv/bin/ruff check services/api`, `.venv/bin/mypy services/api/clipos_api`. DATABASE_URL-gated pg backup test SKIPs in-sandbox — honest; orchestrator + CI run it (compose pg already in test job).
- NO new deps beyond opentelemetry-api/opentelemetry-sdk (already in venv — ADD to pyproject dependencies).

## Required reading
02_SYSTEM_ARCHITECTURE.md (Observability: OpenTelemetry, Prometheus, Grafana, Loki; optional Sentry), 12_OPERATIONS_RUNBOOK.md (Backup policy baseline: frequent DB backup, daily full, config export, restore tests; alert on backup freshness), 07 + 13 (redact tokens in logs/traces; never store secrets), services/api/clipos_api/{audit.py,audit_trail.py (structured event patterns), infrastructure.py (health)}, .agent/RUNNER_CONVENTIONS.md, docs/development.md.

## Scope boundaries
- No OTLP/Prometheus exporters or collectors deployed (document: modes none|console now, otlp when a collector exists); no Grafana/Loki/Sentry setup (deployment ticket territory).
- No full backup SCHEDULER/cron (primitives + CLI only; scheduling = ops later); encryption-at-rest of backups = future (document per 12 "daily encrypted" — note as roadmap).
- Do NOT modify CLIP-006..012 modules; env-only config (OTEL_SERVICE_NAME default "clipos-api", OTEL_MODE default "none", OTEL_LOG_LEVEL default "INFO").

## Deliverables (exact paths — orchestrator verifies)
1. `services/api/pyproject.toml` — add opentelemetry-api>=1.20, opentelemetry-sdk>=1.20 (floors from installed).
2. `services/api/clipos_api/telemetry.py`:
   - `TelemetryConfig` frozen from_env() (service_name, mode none|console, log_level; validation errors on bad mode).
   - `init_telemetry(config=None) -> TelemetryHandle` — IDEMPOTENT (second call returns the same handle, does not reconfigure providers; test proves providers unchanged); mode none = no-op providers (still usable APIs); mode console = console-span/metric readers via sdk; handle exposes `tracer(name)`, `meter(name)`, `shutdown()`.
   - `SecretRedactionFilter(logging.Filter)` — rewrites record.msg/args: regex-mask `Bearer\s+\S+`, `sk-[A-Za-z0-9]{8,}`, `AKIA[A-Z0-9]{16}`, `(?<=://)[^@\s/]+:[^@\s/]+@` (URL creds → `***:***@`), `password=...`/`token=...` style key=value → key=[REDACTED]; applied via `install_redaction(handler=None)` (idempotent — handler already filtered = no double filter).
   - `get_handle()` returns current or None; `counter(name, unit)`/`histogram(...)` convenience using handle meter (no-op meter when uninitialized — never raise).
3. `services/api/clipos_api/backup.py` + `scripts/backup.py`:
   - `BackupManifest` frozen pydantic: version=1, id, created_at, kind ("files"|"postgres"), items (list of {path, sha256, size_bytes}), total_bytes, note optional.
   - `create_files_manifest(paths, dest_dir)` — hash every existing file (sha256), write `<dest>/manifest.json`, return manifest (idempotent-ish: same inputs → same items/hashes; created_at/id differ — document).
   - `verify_manifest(manifest_path) -> list[str]` — [] when all hashes match; else one problem line per tampered/missing file.
   - `pg_dump_backup(dest_dir, database_url)` — subprocess `pg_dump --format=custom --file <dest>/pg.dump <url>` (use env DATABASE_URL if arg omitted), returns manifest kind=postgres with dump sha256; raise `BackupError` with captured stderr on failure; NEVER log the URL (redact in messages).
   - `scripts/backup.py` CLI: `create --dir ... [paths...]`, `verify <manifest.json>`, `pgdump --dir ... ` (DATABASE_URL from env) — exit 0/1, prints JSON line results.
4. `services/api/tests/test_telemetry.py` — no network: from_env validation (bad mode raises); init idempotent (call twice → same tracer provider object; mode none then handle usable: tracer.start_span works, ends without export errors); redaction filter masks ALL pattern classes (bearer/sk-/AKIA/url-creds/password kv) leaving normal text intact; install_redaction idempotent (filter attached once — count on handler.filters); counter/histogram no-op safe before init; span creation with attributes doesn't crash in none mode.
5. `services/api/tests/test_backup.py` — tmp dirs: manifest create→verify [] ; tampered file → verify lists it ; deleted file → verify lists it ; missing source raises clear error ; pgdump: DATABASE_URL-gated test that runs against compose pg (skip when unset) asserting dump file exists + manifest verifies + URL absent from any log/output captured.
6. CI `.github/workflows/baseline-ci.yml` — no new services needed (DATABASE_URL already set in test job); ensure nothing else changes; if pytest needs `-m` deselect nothing (gated tests self-skip). YAML valid, report needs all four.
7. `docs/development.md` — "Telemetry & backup" paragraph (init usage, redaction guarantee, backup CLI examples, roadmap: otlp/prometheus exporter + scheduler + encryption per 12).
8. `.agent/dryrun/CLIP-013_completion_report.json` with REAL gate totals.

## Acceptance (orchestrator re-runs outside sandbox — incl. REAL Postgres pgdump)
- `.venv/bin/pytest -q services/api` green in sandbox (pgdump SKIPs honest); orchestrator with DATABASE_URL: pgdump test green, full suite green.
- ruff format --check + check + mypy clean; compileall green.
- validate_handoff, validate_state, state_audit verify, architecture_guard, validate_packet, secret_scan, workflow YAML — green; CI 4 jobs green after merge.
- No secret literals; telemetry init provably idempotent; existing tests untouched and passing.

## Allowed scope
paths: services/api/{pyproject.toml,clipos_api/telemetry.py,clipos_api/backup.py,scripts/backup.py,tests/test_telemetry.py,tests/test_backup.py}, .github/workflows/baseline-ci.yml, docs/development.md, .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; deploying collectors/exporters; backup scheduler/encryption claims; routes; modifying CLIP-006..012 modules; fabricating results.

## Required gates (runnable here — RUN THEM)
- .venv/bin/pytest -q services/api (green; pgdump SKIP)
- .venv/bin/ruff format services/api && .venv/bin/ruff check --fix services/api && .venv/bin/ruff check services/api && .venv/bin/ruff format --check services/api
- .venv/bin/mypy services/api/clipos_api
- python3 -m compileall services/api scripts
- python3 scripts/validate_handoff.py; python3 scripts/validate_state.py; python3 scripts/state_audit.py verify; python3 scripts/architecture_guard.py; python3 scripts/validate_packet.py .agent/task_packet_CLIP-013.md
- bash scripts/secret_scan.sh; yaml parse of baseline-ci.yml

## Completion output
Completion report + concise summary; no fabrication.
