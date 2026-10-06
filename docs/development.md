# ClipOS local development

The M0 foundation intentionally contains no product features. It establishes package boundaries, typed contracts, local infrastructure definitions, and repeatable quality commands.

## Environment setup

```bash
make setup
```

This creates `.venv` and installs the API in editable mode with development dependencies. Web type checking uses package installation on demand; if Node is unavailable, record the execution environment blocker and continue with server-side gates.

## Tests, linting, and type checks

```bash
make quality
```

`make quality` is the operator-visible single entry point for format check, lint, mypy, pytest with coverage, pip-audit, `npm audit`, and the OpenAPI contract check. Individual commands remain available:

```bash
make test
make lint
make format
make typecheck
make openapi
```

The Python suite covers the health contract, provider alias rejection, absence of hard-coded vendor/model identifiers, audit records, and the 21 bounded modules. Coverage of `clipos_api` is enforced by `--cov-fail-under` in `services/api/pyproject.toml`. The floor is currently the specification minimum of 60 because the CLIP-003 sandbox had no pytest-cov to measure real coverage; raise it to `measured - 5` (never below 60) on the first fully measured run outside the sandbox.

`mypy` runs pragmatically (`python_version = "3.12"`, `check_untyped_defs = true`, not strict). The only suppression currently in source is `# type: ignore[return-value]` in `services/api/clipos_api/infrastructure.py`, where Docker's real compose JSON may return `Health` as a bare string, a nested object, or omit it entirely; the ignore documents that runtime shape narrowing. Add future suppressions only with a concrete rule and a written reason, and keep this documentation in sync.

## Dependency and security audits

`scripts/dependency_audit.sh` runs `pip-audit -r requirements-audit.txt` and fails on findings. `scripts/audit-ignores.txt` holds checked-in exceptions, one per line, formatted as `<CVE> # justification`; every entry needs a written reason, and findings without an entry fail CI. Never silence findings by editing tool flags or deleting checks. The Python SBOM is generated with `cyclonedx-py` into `sbom/api-python.cdx.json` (gitignored, uploaded as a CI artifact). `npm audit --audit-level=high` runs in `apps/web`.

Note: CLIP-003 added Next.js/React runtime dependencies to `apps/web/package.json` but the sandbox had no Node/npm to regenerate `package-lock.json`. Use `npm install --no-audit --no-fund` in `apps/web` (as CI and the web Dockerfile do) until the lockfile is regenerated, then switch back to `npm ci`.

## OpenAPI contract

```bash
make openapi
```

Exports the FastAPI schema to `openapi.json` (gitignored) and asserts the `/health` path exists. CI runs the same export and assertion in the `quality` job.

## Local infrastructure

```bash
make infra-up
```

This starts PostgreSQL with pgvector, Redis, and MinIO using clearly non-production local credentials, waits for their Compose health checks, and runs the infrastructure health gate. Use `make infra-down` to stop the stack and `make infra-health` to re-check it without changing container state; the script is read-only and repeatable. Optional API and web containers are grouped behind the Compose `app` profile and are not started by the M0 skeleton.

The Compose project name is fixed to `clipos-dev`, so repeated commands address the same stack regardless of the shell directory. Host ports are intentionally non-colliding: PostgreSQL uses 5433, Redis 6380, and MinIO 9002/9003. Host port 5432 is reserved by the operator's live PostgreSQL service and must never be bound by development infrastructure. Ad-hoc container runs (for example, `scripts/e2e_smoke.sh`) use host port 18099.

`scripts/integration_smoke.sh` is a read-only integration gate against a running stack: it executes a real `psql SELECT 1` and `redis-cli PING` via `docker compose exec`, checks MinIO readiness with `mc ready local`, prints a JSON summary, and exits 0/1. `scripts/e2e_smoke.sh` builds the API image, runs it on port 18099, polls `/health` for up to 30 seconds, asserts the JSON status field, and always removes its container via a trap.

## Architecture & governance

Architecture decisions are recorded in [docs/adr/](adr/README.md); new material decisions (architecture, stack, provider policy, CI policy, security posture) follow the ADR process described there. `BUILD_STATE.json` is validated against `schemas/build_state.schema.json` by `python3 scripts/validate_state.py` (stdlib-only; also cross-checks the ticket backlog and last evidence). `python3 scripts/architecture_guard.py` statically checks the ADR set, the 21 bounded modules, provider neutrality in `providers.py`, the registry contract, and the four required CI jobs. Both run in the CI `validate` job after the secret scan, and `make governance` runs them together with the governance tests. Both validators are read-only: they never mutate `BUILD_STATE.json`, `10_TICKETS.yaml`, or reports.

## Identity/session foundation

The M1 identity/session foundation lives in `services/api/clipos_api/identity.py`: frozen Pydantic v2 contracts (`User`, `Session`), a `SessionStore` Protocol with a thread-safe `InMemorySessionStore`, and an `IdentityService` providing idempotent session creation, rotation, revocation, touch, expiry resolution through an injected clock, CSRF token generation, and secure-cookie attribute helpers. Raw bearer tokens are returned once and only SHA-256 hashes are stored; material session changes are recorded through the shared `clipos_api.audit` module (with an additive `read_events` helper for verification). CLIP-010 will add the PostgreSQL-backed implementation of the same store protocol; no SQL or HTTP layer is in scope here.

## Organization tenancy

The M1 organization tenancy foundation lives in `services/api/clipos_api/tenancy.py`:
frozen Pydantic v2 contracts (`Organization`, `Membership`), `OrganizationStore` and
`MembershipStore` Protocols with thread-safe in-memory implementations, and a
`TenancyService` providing slug-idempotent organization creation, membership lifecycle
with a last-active-owner invariant, and `require_membership` — the server-side
tenant-scoping primitive required by 07 (it returns the active membership or `None`,
and callers must deny on `None`). Roles are a simple owner/admin/member enum for tenancy
management only; RBAC/capabilities arrive with CLIP-008, and CLIP-010 will add the
PostgreSQL-backed store implementations. Material changes are recorded through the
shared `clipos_api.audit` module.

## Authorization

The M1 RBAC/capability foundation lives in `services/api/clipos_api/authorization.py`:
validated `resource:action` permission strings, frozen BUILT-IN roles mapped to the 01
personas (plus CLIP-007 membership roles), and a deny-by-default `AuthorizationService`.
The service resolves the caller through `TenancyService.require_membership`, so only an
ACTIVE membership in the requested organization can grant access; cross-tenant membership
and suspended orgs always deny. Custom role editing and `role_permissions` persistence are
SQL-era work (CLIP-010+); service identity storage is CLIP-009. The module also provides
`CapabilityPolicy` for tool-call allowlists: the default set is empty, so capabilities such
as GPU are never assumed, and each required capability must be explicitly granted. Denied
WRITE-class authorization checks are audited as `authz.denied`; allows are not audited.

## Cache & rate limiting

The API uses Redis for cache and rate-limit primitives. Configuration is
environment-driven:

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `CACHE_REDIS_URL` | yes | — | Redis URL (CI uses the Compose host mapping on port 6380) |
| `CACHE_KEY_PREFIX` | no | `clipos:` | Namespace applied to every cache key |
| `CACHE_DEFAULT_TTL_SECONDS` | no | `300` | Default cache TTL when `set` omits TTL |
| `RATE_LIMIT_KEY_PREFIX` | no | `clipos:` | Prefix for fixed-window rate-limit keys |

`clipos_api.cache` exposes `CacheConfig`, the `KeyValueCache` Protocol, RedisCache, and an in-memory fallback with an injected clock. `clipos_api.rate_limit` exposes a validated `RateLimitPolicy`, `RateLimitResult`, the `RateLimiter` Protocol, RedisRateLimiter, and an equivalent in-memory twin. Both rate limiters count by fixed epoch windows: after `limit` calls, `allow()` denies with a positive retry interval until the next window starts. Redis uses `INCR` plus `EXPIRE` on the first increment; a crash between those calls can leave the initial key without TTL, and sliding-window/token-bucket behavior is intentionally future work. Future API tickets will wire `limiter.allow(identity_key)` into routes/middleware and treat `allowed=False` as an HTTP rejection; these modules expose no HTTP behavior yet. `CACHE_REDIS_URL`-gated integration tests run in CI against the Compose Redis service.

## Object storage

The API stores media and documents in S3-compatible object storage: MinIO locally
(Compose service, host port 9002, console 9003) and S3, Cloudflare R2, or any
compatible endpoint in production. Configuration is environment-driven only —
credentials are never written to code or logs:

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `STORAGE_ENDPOINT` | yes | — | S3-compatible endpoint URL (e.g. `http://localhost:9002` for MinIO) |
| `STORAGE_ACCESS_KEY` | yes | — | Access key reference |
| `STORAGE_SECRET_KEY` | yes | — | Secret key reference (redacted in all repr/log output) |
| `STORAGE_BUCKET` | yes | — | Target bucket |
| `STORAGE_REGION` | no | `us-east-1` | Optional region |
| `STORAGE_PRESIGN_TTL_SECONDS` | no | `3600` | Default TTL for signed URLs |

`clipos_api.storage.S3ObjectStorage` implements the `ObjectStorage` protocol:
idempotent `put`/`get`/`delete`/`ensure_bucket`, content-type/size upload policy
(default allowlist: png/jpeg/webp, mp4/webm, pdf; 100 MB cap), and presigned
GET/PUT URLs:

```python
storage = S3ObjectStorage(StorageConfig.from_env())
storage.ensure_bucket()
storage.put("media/clip.png", payload, "image/png")
url = storage.presign_get("media/clip.png", ttl_seconds=300)
```

Missing objects raise `ObjectNotFound`; botocore errors never escape the module
API. Unit tests use an injected fake client; MinIO integration tests activate
when `STORAGE_ENDPOINT` is set (the CI test job sets it against the Compose
MinIO service). Multipart/resumable uploads via Temporal and CDN delivery are
future work.

## Temporal (dev/self-host)

The dev Compose stack runs self-hosted Temporal for future durable orchestration
(single-server topology per the system architecture): `temporalio/auto-setup:1.29.7`
initializes and serves the frontend on host port 7233 (gRPC health endpoint) against
the Compose PostgreSQL instance in a dedicated `temporal` database, and
`temporalio/ui:2.55.0` serves the web UI on host port 8233 (container 8088).
`scripts/infrastructure_health.sh` gates on the health-checked Compose services plus
direct probes of gRPC 7233 inside the Compose network and UI HTTP 8233, so the CI
test job already waits for Temporal alongside PostgreSQL, Redis, and MinIO.

Configuration is environment-only, validated by
`clipos_api.temporal_config.TemporalConfig.from_env()`: `TEMPORAL_ADDRESS`
(default `localhost:7233`), `TEMPORAL_NAMESPACE` (default `clipos-dev`),
`TEMPORAL_UI_URL` (default `http://localhost:8233`), and
`TEMPORAL_CONNECT_TIMEOUT` seconds (default 5). Malformed addresses and
non-positive timeouts raise `ValueError`. The connectivity helper
`probe_temporal(config)` connects with `temporalio.Client.connect` inside the
configured timeout and returns a frozen `TemporalProbeResult` (`ok`, sanitized
`detail`); it never raises on unreachable servers and handles no credentials:

```python
config = TemporalConfig.from_env()
result = await probe_temporal(config)
assert result.ok, result.detail
```

The CI test job sets `TEMPORAL_ADDRESS: localhost:7233`, so the live probe test
runs there. Dev self-host auth and mTLS are deferred and must be designed before
any non-dev deployment.

## Workflow framework & conventions

The M3 workflow foundation lives in three additive modules. `clipos_api.workflow_framework`
holds the typed conventions from 06_WORKFLOW_ORCHESTRATION.md: `WorkflowMetadata`
(frozen Pydantic: `organization_id`, optional `brand_id`/`content_id`, `policy_version`
matching `v1`, `v1.2`, or `v1.2.3`-style versions, and `correlation_id`, with `as_dict()`
for carrying across workflow inputs), the four `business_key()` idempotency formats
(`publish:{account_id}:{variant_id}:{scheduled_slot}`,
`render:{project_id}:{render_version}`,
`rights-review:{source_item_id}:{rights_version}`, and
`analytics:{publication_id}:{window}`; missing or extra parts raise `ValueError`),
the seven retry categories with `retry_policy_for()` mappings, `durable_wait()`,
`ApprovalRequest`/`approval_expired()`, and the `audit_workflow_event()` hook that
writes `workflow.<name>` events through the shared audit module.

Retry behavior by category:

| Category | Policy |
| --- | --- |
| `network_transient` | exponential backoff: 2s initial, coefficient 2.0, 60s cap, 10 attempts |
| `rate_limit` | 30s initial backoff, 5 attempts |
| `auth_expired` | 2 attempts (single refresh) |
| `invalid_request` | 1 attempt, no blind retry |
| `policy_rejection` | 1 attempt, no blind retry |
| `media_corruption` | 1 attempt, terminal/quarantine |
| `insufficient_rights` | 1 attempt, terminal/quarantine |

`durable_wait(seconds)` is a thin documented wrapper around
`temporalio.workflow.sleep` and MUST only be called from workflow context;
`asyncio.sleep` is forbidden in workflow code. Approval requests carry action,
exact risk, non-empty evidence, a tz-aware future UTC expiry, the approver role,
and `timeout_behavior: "deny"` (the default deny on timeout is enforced by
`approval_expired`).

`clipos_api.worker` is the worker bootstrap: `WorkerConfig.from_env()` reuses
`TemporalConfig` for `TEMPORAL_ADDRESS`/`TEMPORAL_NAMESPACE` and adds
`TEMPORAL_TASK_QUEUE` (default `clipos-core`) and
`TEMPORAL_MAX_CONCURRENT_ACTIVITIES` (default 100); `connect_client`,
`build_worker`, and `run_worker_until_stopped` provide connect/assemble/graceful
shutdown with no stored credentials and no provider identifiers.

`clipos_api.reference_workflow` is the framework self-test: it validates its
`WorkflowMetadata`, runs the idempotent `record_idempotent_step` activity keyed by a
`render` business key, sleeps on durable timers, and resolves the approval step with
default-deny semantics. The in-memory idempotency store is injected at worker bootstrap
(`use_idempotency_store`), so two workflow executions sharing a worker and the same
business key prove `first_run: True` then `False`. Run it against a live server with:

```python
TEMPORAL_ADDRESS=localhost:7233 .venv/bin/pytest -q services/api/tests/test_workflow_integration.py
```

The integration test is skipped honestly without `TEMPORAL_ADDRESS`; the CI test job
already sets it, so the workflow executes there against the Compose Temporal service.
## Autonomous runner

Ticket execution follows the canonical conventions in
[.agent/RUNNER_CONVENTIONS.md](../.agent/RUNNER_CONVENTIONS.md): lease acquire, worktree,
validated task packet, observed ground-truth evidence, gate re-runs, land, CI green,
state advance, lease release. Task packets are checked with
`python3 scripts/validate_packet.py <path>`; the CI `validate` job and `make governance`
validate the packet template. Material state transitions are recorded in the append-only
`docs/reports/STATE_AUDIT.jsonl` via `python3 scripts/state_audit.py`
(`append`/`verify`/`bootstrap`; verify must cover every completed ticket before state is
considered complete). Operators inspect the audit directly:
`cat docs/reports/STATE_AUDIT.jsonl` (one JSON object per line, oldest first; existing
lines are never rewritten).

## Persistence & migrations

The API's persistence foundation uses SQLAlchemy 2.x models with Alembic migrations in
`services/api/migrations/`. The database URL is read from `DATABASE_URL`; when unset,
`alembic` falls back to `sqlite:///.alembic-dev.db` for local offline SQL rendering only.
To run migrations locally, start `docker compose -f docker-compose.dev.yml up -d` and run
`DATABASE_URL=postgresql+psycopg://clipos_dev:clipos_dev_local_only@localhost:5433/clipos_dev
alembic -c services/api/alembic.ini upgrade head` (the Compose credentials are
non-production by design). Tests always exercise SQLite in-memory; PostgreSQL-gated tests
activate when `DATABASE_URL` is set. Repositories currently available are the generic
`BaseRepository`, record-level `OrganizationRepository`/`MembershipRepository`, and the
protocol-conformant `SQLOrganizationStore`/`SQLMembershipStore` in
`services/api/clipos_api/repository.py`; in-memory stores remain the default for services.

## CI lane matrix

The 16 lanes required by `11_CI_CD_DEPLOYMENT.md`, mapped to the current implementation:

| # | Lane | Status |
|---|------|--------|
| 1 | Formatting/lint | Implemented — `test` job: `ruff format --check` + `ruff check` |
| 2 | Python type/static checks | Implemented — `test` job: `mypy services/api/clipos_api` |
| 3 | TypeScript typecheck | Implemented — `test` job: `npx tsc --noEmit` in `apps/web` |
| 4 | Unit tests (+coverage) | Implemented — `test` job: `pytest` with `pytest-cov`, fail-under floor |
| 5 | Database migration tests | Implemented — `test` job: `alembic upgrade head` against compose PostgreSQL, then `pytest` with `DATABASE_URL` so PG-gated migration tests run; SQLite tests always run |
| 6 | Integration tests (PostgreSQL/Redis/object storage/Temporal) | Partially implemented — `test` job: `scripts/integration_smoke.sh` against live PostgreSQL/Redis/MinIO; Temporal deferred — not in dev stack yet (M3) |
| 7 | Tenant/RBAC/security regression tests | Partially implemented — tenant/RBAC/security regression tests exist; external-provider and public-publication security gates activate in later tickets |
| 8 | Workflow replay/idempotency tests | Deferred — no workflow engine yet (M3) |
| 9 | Agent schema/eval smoke tests | Deferred — agent/model registries arrive in M4/M5; registry unit tests exist but no eval harness yet |
| 10 | Secret scan | Implemented — `validate` job: `scripts/secret_scan.sh` |
| 11 | Dependency/SBOM vulnerability scan | Implemented — `quality` job: pip-audit, cyclonedx SBOM, `npm audit --audit-level=high` |
| 12 | Container build | Implemented — `build` job: `docker compose ... --profile app build` |
| 13 | API contract/OpenAPI checks | Implemented — `quality` job: export `openapi.json`, assert `/health` |
| 14 | Frontend build | Implemented — `build` job: `npm install && npm run build` in `apps/web` |
| 15 | E2E smoke tests | Implemented — `build` job: `scripts/e2e_smoke.sh` against the built API image |
| 16 | Provider integration tests behind secrets | Deferred — provider credentials/human gates not configured yet (M5+); the lane must activate behind explicit secrets |

## Containers

`services/api/Dockerfile` builds a non-root Python 3.12 image running uvicorn on port 8000. `apps/web/Dockerfile` builds a Next.js standalone image on Node 22. CI builds both via the Compose `app` profile.

## M0 evidence map

- Repository/package skeleton: `apps/web/`, `services/api/`.
- Provider abstraction and logical alias contract: `services/api/clipos_api/providers.py`, `registry.py`.
- Bounded modules: `services/api/clipos_api/modules.py`.
- Operator-visible audit contract: `services/api/clipos_api/audit.py`.
- Development topology: `docker-compose.dev.yml`.
- CI lanes and scan tooling: `.github/workflows/baseline-ci.yml`, `scripts/dependency_audit.sh`, `scripts/integration_smoke.sh`, `scripts/e2e_smoke.sh`.
- Required gates: packet commands; results are recorded in `.agent/dryrun/CLIP-003_completion_report.json`.

## Service identities & immutable audit

Service identities (CLIP-009) are machine principals — Service Agents, integrations, and workers — with typed frozen contracts (`ServiceIdentityKind`, `ServiceIdentityState`) and unique slug-like names. Credentials are stored as **references only** (e.g. `vault:prod/<name>`); raw secrets never enter the module, per the 07 "secrets manager references only" control. Service tokens follow the CLIP-006 identity pattern: a raw token is returned to the caller exactly once on creation or rotation, and only its SHA-256 hash is persisted. Token rotation invalidates the previous token immediately. `disable`/`enable` are idempotent state transitions, and disabled identities fail authentication.

The immutable audit trail (`audit_trail.py`) is an append-only, hash-chained ledger: every event embeds a monotonic `seq`, the SHA-256 hash of its immediate predecessor (`prev_hash`), and the SHA-256 of its own canonical payload (`event_hash` = sha256 of `"{seq}|{timestamp}|{action}|{actor}|{subject}|{canonical_json(details)}|{prev_hash}"` with sorted-key compact JSON). Chain verification detects modification (hash mismatch), deletion (seq gap + detached event), and reordering (prev_hash mismatch). The `InMemoryAuditTrailStore` public interface is append-only — no update, delete, reverse, pop, or clear — enforcing immutability at the type level. `record_from_legacy` bridges `clipos_api.audit` file-based records into the hash chain. PostgreSQL persistence for `service_identities` and `audit_events` tables lands in CLIP-010.


## Telemetry & backup

`clipos_api.telemetry.init_telemetry()` is process-wide and idempotent; it reads
`OTEL_SERVICE_NAME` (default `clipos-api`), `OTEL_MODE` (`none` default, or
`console`), and `OTEL_LOG_LEVEL` (default `INFO`). `none` keeps the OTel API in
its no-op provider mode, so application code can always call
`handle.tracer("name")` / `handle.meter("name")`; `console` exports spans and
metrics to stdout for local debugging. There is no remote collector yet, so
OTLP/Prometheus exporters are intentionally absent until the architecture's
collector deployment exists. `SecretRedactionFilter` (install with
`install_redaction()`) masks Bearer tokens, `sk-`/AKIA-style keys, URL
credentials, and `password=`/`token=` values in log records.

Backup primitives in `clipos_api.backup` hash files into a manifest and re-hash
to detect tampering:

    python scripts/backup.py create backups/ file1.txt file2.txt
    python scripts/backup.py verify backups/manifest.json
    DATABASE_URL=... python scripts/backup.py pgdump backups/

`pgdump` requires `DATABASE_URL`, never logs it, and runs `pg_dump
--format=custom` into the destination directory. These are primitives only:
scheduling (cron), encrypted backups at rest (12 runbook roadmap), and
object-storage upload are future operations work.
