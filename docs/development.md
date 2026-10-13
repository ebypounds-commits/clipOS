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

## Outbox & idempotency

Database mutations and emitted domain events share one transaction: the application writes its
business row and calls `OutboxStore.enqueue(session, event)` in the same SQLAlchemy session, then
commits once. That shared commit is the transactional guarantee from `02_SYSTEM_ARCHITECTURE.md`
§9 — an event can never exist after a rolled-back mutation, and a mutation cannot commit without
its event. The `enqueue_in_transaction(event)` helper is for a standalone event transaction;
the caller-owned `enqueue` form is the normal path. `available_at` doubles as the next delivery
time and lease deadline: a worker claims due pending rows, increments `attempts`, and postpones
the row for the lease interval. PostgreSQL claims use `SELECT ... FOR UPDATE SKIP LOCKED`, so
concurrent workers receive disjoint batches; failed delivery schedules
`available_at = now + retry_backoff_seconds * 2**(attempts - 1)` and becomes terminal `failed`
at `max_attempts`. Statuses are `pending`, `published`, and `failed`; `stats()` exposes counts.
Audit records (`outbox.enqueued`, `outbox.published`, `outbox.failed`) carry identifiers,
attempt counts, and a SHA-256 payload hash — never payload contents.

External writes use `idempotency_records`. `claim` uses `INSERT ... ON CONFLICT (key) DO NOTHING`
and rowcount as the race-safe decision; a concurrent successful insert means the key is already
owned. A succeeded record replays its stored response; an active lease reports `in_progress`;
a failed or expired lease is reclaimable and increments `attempts`. Complete, fail, and recovery
are single atomic updates, so a crash can leave only the owned state, never a half-created
external write. `request_hash` mismatch on a succeeded key raises `ValueError` rather than
replaying the wrong request. Business-key linkage reuses the exact formats produced by
`clipos_api.workflow_framework.business_key`; this module never invents another format.
`execute_idempotent` composes the lifecycle:

```python
response = execute_idempotent(
    store=store,
    key="publish:acct-1:variant-9:slot-42",
    scope="publishing",
    request_hash=request_digest(provider_request),
    fn=lambda: provider.publish(provider_request),
    lock_seconds=300,
    business_key=workflow_framework.business_key(
        "publish", account_id="acct-1", variant_id="variant-9", scheduled_slot="slot-42"
    ),
)
```

The generic tables are `outbox_events` and `idempotency_records` (migration `0002`). The
publishing-specific `publishing_idempotency` row wiring lands with the publishing tickets.
Delivery is currently database-backed; NATS JetStream remains a future substitution only when
event throughput/decoupling justifies it, as specified by §Events.

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

## Local model adapter (M5)

The LLM gateway dispatches through provider adapters; `local_adapter.py` adds the
local-inference route. `LocalProviderAdapter` speaks a plain OpenAI-compatible
chat-completions protocol over HTTP (`httpx`, already a dependency) to whatever
base URL is configured — no provider SDK is imported and no provider identifier
is embedded. Startup wiring is opt-in: when `LOCAL_ADAPTER_BASE_URL` is set,
`build_llm_gateway` registers `build_local_adapter` under the provider name from
`LOCAL_ADAPTER_PROVIDER` (default `local`); unset, no adapter is registered.
Timeouts come from `LOCAL_ADAPTER_TIMEOUT_SECONDS` (default 30s). Connection,
HTTP, and timeout failures raise `LocalProviderDispatchError` (an
`LLMDispatchError`), so the gateway degrades the route and walks the next
entry in the alias fallback chain. `apps/web/app/models/page.tsx` is a server
component that renders alias routes — provider names, priorities, and health
states — from `GET /models` only, following the alias-only rule.

## Rights ledger and license evidence (M7)

`clipos_api.rights_ledger` builds on the CLIP-030 foundations and adds the
operational rights ledger. `RightsLedgerStore` persists rights records,
licenses, license evidence, attribution requirements, and append-only rights
events (migration `0012`, CREATE-only). Rights records are org-scoped and
idempotent per `(item_id, org_id)`; they validate that the referenced source
item exists in the same organization, and their initial status is constrained
to `UNKNOWN` or `REVIEW_REQUIRED` — `CLEAR` is never assigned automatically.

Status transitions accept only the spec enum values and append a `rights_events`
row plus an audited `rights_ledger.status_changed` entry carrying
`from_status`, `to_status`, and an optional `reason_ref` (references only, no
content). Licenses are idempotent per `(record_id, terms_ref, starts_at)`;
license evidence is append-only (update attempts are rejected) and stores file
or blob references, never inline content. Attribution requirements are
idempotent per `(record_id, applies_to, requirement)`.

`effective_status(record)` derives the current state from license windows and
recorded transitions with an injectable clock: an expired license window makes
the record `EXPIRED`; an active license makes it `CLEAR`; otherwise the stored
status stands. Listing and event reads are organization-scoped. The module
exposes operator routes under `/rights-records` and `/licenses`, wired in
`main.py`, following the same store/router pattern as the other registries.

## Clip scoring (M10)

clipos_api.clip_scores (CLIP-043) implements deterministic multi-factor scoring
for clip candidates over the clip_scores table (migration  024, CREATE-only,
unique per (org_id, candidate_id)). Four pure component scorers derive values in
0..1 from data already in the database: length_fit (candidate duration against
the caller's ideal window), hook_coverage and claim_coverage (union coverage
of overlapping semantic_segments of that kind within the candidate window,
merged so double-covered regions never count twice), and 	ranscript_density
(words per second from overlapping 	ranscript_segments against a caller-supplied
target rate). score_clip combines caller-supplied positive weights into a
0..100 total — the same weighted-sum contract as CLIP-036 opportunity scoring;
weights must name known factors (COMPONENT_FACTORS) and missing components count
as zero. No LLM, network, or provider identifier is involved; the optional
scorer_alias column records a registry-style alias for the scorer that produced
the row, never a vendor/model string.

ClipScoreStore.record_score is idempotent per candidate: an identical re-score
returns the stored row; different components/total raise (409 via route). Scoring
transitions the candidate NEW -> SCORED and audits clip_scores.recorded plus
clip_candidate.scored; decided candidates (APPROVED/REJECTED) are frozen and
reject further scoring. Routes follow the CLIP-033 pattern (real runtime imports,
Depends providers before decorators): POST /clip-candidates/{candidate_id}/scores,
GET /clip-candidates/{candidate_id}/scores, GET /clip-scores?org_id=.

## Candidate dedup and fingerprints (M10)

clipos_api.clip_dedup (CLIP-044) implements deterministic duplicate/content
fingerprint checks for clip candidates over the candidate_fingerprints table
(migration  025, CREATE-only, unique per (org_id, candidate_id, kind)).
Two independent signals: a **WINDOW** sha256 over the canonical
window:asset:start-end identity (always persisted), and a **TRANSCRIPT**
sha256 over the normalized (lowercased, whitespace-collapsed) transcript text
inside the candidate window from the primary transcript (earliest
created_at, segments ordered by seq so the hash is byte-stable; never
persisted when the window has no text, so absent content can never collide).
Pure helpers 
ormalize_text, overlap_over_shorter (overlap / shorter
window length), window_fingerprint, and 	ranscript_fingerprint are
side-effect free and fully unit-tested.

Fingerprints are persisted facts; duplicate relations are derived on demand:
POST /clip-candidates/{candidate_id}/duplicate-check persists fingerprints
idempotent per key (identical re-run no-op, different value -> 409),
compares the window against other candidates on the same asset
(overlap_over_shorter >= threshold, threshold caller-supplied in (0..1],
default 0.5), and looks up org-wide TRANSCRIPT collisions held by other
candidates. The report returns both fingerprint rows and window/content
findings; the run is audited as clip_candidates.duplicate_check and each
persist as candidate_fingerprints.persisted. GET
/clip-candidates/{candidate_id}/fingerprints lists stored facts (404 on
unknown candidate). This is candidate-level dedup and does not duplicate the
manifest-level content_fingerprints from CLIP-032 (provenance scope).
No LLM, network, or provider identifier is involved.

## Review tooling and benchmark eval set (M10)

clipos_api.clip_review (CLIP-045) implements the operator review half of
M10: append-only APPROVE/REJECT decisions for clip candidates over the
candidate_decisions table (migration  026, CREATE-only, unique
(candidate_id, decision) — one row per kind, mirroring CLIP-036
opportunity_decisions). ClipReviewStore.record_decision is idempotent per
(kind, actor): identical re-record returns the stored row, a different actor
raises (409). Recording transitions the candidate to APPROVED/REJECTED
(CLIP-043 scoring treats decided candidates as frozen) and audits
clip_candidates.decided. Routes: POST /clip-candidates/{candidate_id}/decisions
(404/409) and GET /clip-candidates/{candidate_id}/decisions.

Candidate responses now use ClipCandidateView (adds candidate_id and
status to the CLIP-042 fields) so review surfaces can reference rows and
render lifecycle state; the create request body remains
ClipCandidateCreate. The web 
eviews page (/reviews?org=...) is a
server component that loads candidates and scores, with a client board
(ReviewBoard) for scoring (equal weights), duplicate checks
(threshold input), and APPROVE/REJECT decisions (actor input) — organization
comes from ?org= or NEXT_PUBLIC_ORG_ID.

The benchmark eval set is services/api/tests/fixtures/clip_benchmark.json
(version 1): frozen expectations for the deterministic helpers — 9 score
cases (hand-computed components + totals), 6 overlap cases, and 5
fingerprint cases (canonical hashlib sha256 outputs).
	ests/test_clip_benchmark.py executes every case and fails on any behavior
regression, with a floor assertion so the set cannot silently shrink to
nothing. No LLM, network, or provider identifier is involved anywhere in
this ticket.

## Research packets, claims, and claim sources (M11)

clipos_api.research (CLIP-046) implements the first M11 content-planning
model from  3_DOMAIN_DATA_MODEL.md: 
esearch_packets group findings for
a topic (optional 	opic_id reference to the M8 topics table), claims
carry statement rows inside a packet with an UNVERIFIED -> VERIFIED /
DISPUTED veracity lifecycle, and claim_sources link each claim to source
references — references only (source_ref <= 512 chars, quote_ref
optional), never source content. Migration  027 (CREATE-only, 0026 ->
0027) creates all three tables with unique indexes on
(org_id, research_packet_id), (org_id, claim_id), and
(claim_id, source_ref).

Idempotency mirrors CLIP-016 opportunity registration: packets and claims
register on client-supplied natural ids (identical re-register returns the
stored row; different fields raise -> 409); source links are idempotent per
(claim_id, source_ref) (identical re-link no-op; different support or
quote_ref -> 409). Claims require an existing packet in the same org
(404); veracity changes are audited as claims.veracity_changed with
actor/reason references, same-value sets are no-ops, and reopening to
UNVERIFIED is allowed. Registration audits: 
esearch_packets.registered,
claims.registered, claim_sources.linked.

Routes (CLIP-033 pattern: real runtime imports, providers before
decorators): POST/GET /research-packets, POST/GET
/research-packets/{id}/claims (path/body id must match -> 422),
POST/GET /claims/{id}/sources, POST /claims/{id}/veracity. Claim
creation always starts UNVERIFIED — verification must travel through the
audited veracity route. No LLM, network, or provider identifier is
involved.

## Content briefs and the plan engine (M11)

clipos_api.content_planning (CLIP-047) implements PRD domain E's brief
and content-plan layer over the CLIP-046 research model. content_briefs
(brief_id client natural id, idempotent per (org_id, brief_id), unique
index) ground a production ask in one research packet — registration
requires the packet to exist (404) — and carry 	itle, objective, and a
platforms list (<= 16 entries, each 1..64 chars, generic slugs). The
brief/plan engine itself is derive_plan_payload, a pure function: given a
brief and its packet's **VERIFIED** claims it emits a stable payload
(sorted claim_ids, claim_count, echoed objective/platforms/refs).

Factuality is fail-closed: generate_plan refuses (422) when the linked
packet yields no VERIFIED claims — UNVERIFIED and DISPUTED claims are
never planned. Plans are append-versioned per brief
(content_plans, unique (org_id, brief_id, version), migration  028,
0027 -> 0028): identical re-derivation returns the latest version untouched;
changed claim state mints the next version and audits
content_plans.generated (version + claim_count). Brief registration
audits content_briefs.registered.

Routes (CLIP-033 pattern): POST/GET /content-briefs, POST
/content-briefs/{brief_id}/plan (404 brief/packet, 422 no verified
claims), GET /content-briefs/{brief_id}/plan (latest version or null).
No LLM, network, or provider identifier participates — plan derivation is
deterministic software.

## Hooks, scripts, and script versioning (M11)

clipos_api.script_hooks (CLIP-048) adds the creative-asset layer from
PRD domain E. hooks are attention lines for a content brief: registration
is idempotent on the semantic key (org_id, brief_id, text) (unique index)
— the same hook text for the same brief is the same hook — and requires
the brief to exist (404); audited as hooks.registered. scripts are
versioned bodies grounded in one brief (client script_id, idempotent per
(org_id, script_id), conflict on different brief/title -> 409,
current_version starts at 0, audited scripts.registered).
script_versions (migration  029, 0028 -> 0029) is the append-only
history: save_version mints version N+1 for a changed body, bumps
scripts.current_version, and audits scripts.version_appended; an
identical body re-save (regardless of author) returns the latest version
untouched. Bodies are capped at 8192 chars.

Routes (CLIP-033 pattern, _map_error 404/409/422): POST /hooks,
GET /hooks?org_id=&brief_id= (404 unknown brief), POST/GET /scripts,
GET /scripts/{script_id}, POST /scripts/{script_id}/versions,
GET /scripts/{script_id}/versions (404 unknown script). No LLM,
network, or provider identifier is involved.

## Factuality evaluations and transformation records (M11 final)

clipos_api.content_evals (CLIP-049) closes M11 with the two evaluation
artifacts. **Factuality evaluations** (actuality_evaluations, migration
 030) are append-only PASS/FAIL/INCONCLUSIVE rows against a CLIP-046
claim, idempotent per (org_id, eval_id) (identical re-record no-op,
different fields -> 409; unknown claim -> 404). Recording applies the
deterministic veracity mapping **in the same transaction**: PASS ->
VERIFIED, FAIL -> DISPUTED, INCONCLUSIVE -> unchanged (fail-safe), auditing
claims.veracity_changed with 
eason_ref equal to the evaluation id so
every veracity move points at its evidence; a second PASS after the claim
is already VERIFIED records the evaluation without re-transitioning.
method_ref is a free reference — a manual-review label or a Model
Registry alias for model-assisted runs — never a hard-coded provider
string; evidence_ref stores a reference only.

**Transformation records** (	ransformation_records, same migration)
implement the 07 rights-chain row: rom_ref -> 	o_ref -> method for
one brief (refs <= 255 chars to keep the unique index under the PG btree
entry limit), idempotent per (org_id, transformation_id), brief
existence gated (404), audited 	ransformation_records.registered.
Together they maintain the source -> transcript -> script -> render chain
mandated by 07_SECURITY.

Routes (CLIP-033 pattern): POST/GET /factuality-evaluations
(claim-scoped GET 404s) and POST/GET /transformation-records
(brief-scoped GET 404s). Audits: actuality_evaluations.recorded,
claims.veracity_changed, 	ransformation_records.registered. No LLM,
network, or provider identifier participates.

## Render project DSL (M12)

clipos_api.render_project (CLIP-050) introduces the declarative document
that drives the media studio: a render project pins one content brief to a
validated render **spec** (the DSL). Spec shape: ersion (must be 1),
inputs (candidate_id required), composition (spect in
9:16/16:9/1:1, duration_ms 1000..600000, optional captions
{enabled, style_ref} and udio_normalization {enabled, target_lufs
-24..-5 — target required when enabled}), and outputs (1..8 objects each
with a unique platform slug). Unknown top-level keys are rejected so
typos fail loudly; nested unknown keys are tolerated for forward
compatibility.

alidate_render_spec(spec) -> list[str] is the pure, side-effect-free
validator — the single source of truth for spec validity, exercised both
by registration (invalid spec -> 422 carrying the error list, nothing
persisted) and by the dry-run route POST /render-specs/validate (returns
{valid, errors} with no database access at all). 
ender_projects
(migration  031, 0030 -> 0031) stores the validated document, idempotent
per (org_id, render_project_id), brief-gated (404), conflict on changed
fields (409), audited 
ender_projects.registered with spec version and
output count. Platform entries are generic slugs — no provider
identifiers. Routes: POST/GET /render-projects, GET
/render-projects/{render_project_id} (404), plus the pure validator
endpoint. The stored spec is the source document CLIP-051's FFmpeg render
pipeline will consume; captions/audio normalization/aspect variants are
exercised here as spec data, executed by the pipeline ticket.

## FFmpeg worker and render pipeline (M12)

clipos_api.render_pipeline (CLIP-051) executes CLIP-050 specs.
spec_fingerprint is sha256 over canonical sorted-key compact JSON — key
order never changes a render identity. uild_render_argv is the pure
argv builder: trim (-t duration), aspect filter chain from
ASPECT_FILTERS (9:16 -> 1080x1920, 16:9 -> 1920x1080, 1:1 -> 1080x1080,
each scale+pad with black bars), optional loudnorm=I=<target> when audio
normalization is enabled, and -movflags +faststart mp4 output. Caption
burn-in is intentionally deferred to CLIP-052; the deterministic manifest
records captions_requested so the next ticket can act on it. argv lists
are executed with shell=False — there is no injection surface; the
ffmpeg binary is FFMPEG_BIN (default fmpeg), never a provider id.


ender_jobs (migration  032, 0031 -> 0032) run the lifecycle
PENDING -> RUNNING -> SUCCEEDED / FAILED, idempotent per
(org_id, job_id) at creation (bound to one project + spec fingerprint;
same id for a different project -> 409). Execution
(POST /render-jobs/{job_id}/execute) is **workdir-confined** per the
CLIP-039 path policy: input and output must resolve inside
RENDER_WORKDIR (default /tmp/clipos-render) — escapes are 422 — the
input must exist, the output must be .mp4, SUCCEEDED/RUNNING jobs
refuse re-execution (409), FAILED jobs may retry. Success stores the
manifest (fingerprint, argv, paths, flags, timestamps) and audits

ender_jobs.succeeded; failure stores the stderr tail (<=512) as
error_ref and audits 
ender_jobs.failed. Creation audits

ender_jobs.created.

SubprocessRenderRunner is the real backend (timeout
RENDER_FFMPEG_TIMEOUT_SECONDS, default 120s, timeout -> failed job);
tests inject the RenderRunner protocol with a recording double, plus
real-ffmpeg integration tests that build a 	estsrc clip and assert a
non-empty mp4 (skipped when ffmpeg is absent). Routes:
POST /render-projects/{id}/jobs, POST /render-jobs/{id}/execute,
GET /render-jobs/{id}, GET /render-jobs?render_project_id=.

## Captions, audio normalization, and branding (M12)

clipos_api.caption_tracks (CLIP-052) adds the M12 caption layer.
caption_tracks (migration  033, 0032 -> 0033) binds validated timed
text to a render project: registration gates on the project (404), runs
the pure alidate_caption_segments check against the project's
duration_ms (non-empty, <= 200 cues, each 1..200 chars, start < end
inside the render window, sorted non-overlap; violations -> 422 with the
error list), and is idempotent per (org_id, track_id) with conflict on
changed fields (409); audited caption_tracks.registered with segment
count and language. segments_to_srt deterministically renders SubRip
(HH:MM:SS,mmm cues, stable ordering) — the burn-in source.

Pipeline integration in 
ender_pipeline.py (CLIP-051 module, extended):

- escape_filter_path escapes ffmpeg filter-argument metacharacters.
- uild_render_argv gained srt_path / logo_path keywords: SRT cues
  merge into the video chain (spect,subtitles=filename='...'); a logo
  switches the output to -filter_complex with [0:v]<chain>,format=
  yuv420p[base];[base][1:v]overlay=W-w-16:16[v] plus explicit
  -map [v] -map 0:a? (top-right, 16px inset).
- execute_job prep (still before the RUNNING stamp, so failures stay
  retryable 422s): when composition.captions.enabled, the **latest**
  caption track for the project is written to workdir/<job_id>.srt and
  burned in (no track -> clean render, manifest captions_burned=false);
  inputs.logo_ref (workdir-relative, <=256 chars) is confined and must
  exist (missing/escape -> 422, job stays PENDING). Manifest now carries
  captions_burned and logo_overlaid alongside the CLIP-051 fields.
- Audio normalization continues to execute as loudnorm=I=<target_lufs>
  (CLIP-051) and remains covered by tests in combination with captions
  and branding.

Routes: POST/GET /render-projects/{id}/caption-tracks (path/body id
match -> 422) and GET /caption-tracks/{track_id} (404). Tests include
a real-ffmpeg caption burn-in (skipif ffmpeg absent). No LLM, no provider
identifiers anywhere.

## Platform aspect variants, QC, and output manifests (M12 final)

clipos_api.render_variants (CLIP-053) closes M12 with the fanout
layer. POST /render-jobs/{job_id}/variants/execute renders **every**
outputs[] entry of the spec: each output's optional spect override
(defaulting to the spec composition aspect; validated against
{1:1,16:9,9:16} before the RUNNING stamp -> 422 with the job left
PENDING) produces workdir/<job_id>/<platform>.mp4, with platform slugs
restricted to [A-Za-z0-9._-]+ (unsafe slug -> 422) and every path
workdir-confined (CLIP-039 policy). Execution honors the CLIP-051 gates
(input confined, job 404, SUCCEEDED/RUNNING 409, CLIP-052 prep
failures 422) and clears the job's previous 
ender_outputs /
qc_reports rows first so retries never leave orphans.

**QC** runs after each variant: ffprobe (env FFMPEG_BIN sibling
FFPROBE_BIN, default fprobe) feeds the pure evaluate_qc, which
PASSES only when size > 0, probe duration is within ±15% of the spec
window, and width/height exactly match EXPECTED_RESOLUTIONS[aspect]
(1080x1920 / 1920x1080 / 1080x1080). Results persist as qc_reports
(unique per (org_id, output_id), checks JSON); probe failures become
FAILED reports, never crashes. Any ffmpeg or QC failure marks the job
FAILED (error_ref) while keeping completed rows for diagnosis.

**Render manifests**: every successful variant stores a deterministic
per-output manifest on 
ender_outputs (fingerprint, argv, paths,
captions/logo flags, timestamps); the job manifest aggregates
{variant_count, outputs:[{platform, aspect, output_path, qc_status}]}.
Tables: 
ender_outputs + qc_reports (migration  034, 0033 ->
0034, CREATE-only). Audits: 
ender_outputs.rendered,
qc_reports.recorded, plus 
ender_jobs.succeeded/failed.

Also in this ticket: uild_render_argv gained spect_override;
shared CLIP-052 prep extracted to prep_job_assets (used by both
execute paths); the SRT write path is now confine-checked (fixes a
latent traversal via job ids); RenderJobRequest.job_id is slug-pattern
validated. Routes: variants execute (query org_id+input_path),
GET /render-jobs/{id}/outputs (404), GET /render-outputs/{id}/qc-report
(null when absent). Tests include a real-ffmpeg two-platform fanout with
real ffprobe QC (skipif absent). No LLM, no provider identifiers.

## TTS provider adapter and voice assets (M13)

clipos_api.voice_assets (CLIP-054) opens M13 with the CLIP-040
adapter discipline applied to speech synthesis. TTSAdapter is the
provider-neutral Protocol (synthesize(text, voice_ref, model_alias) ->
bytes); MockTTSAdapter is the deterministic stand-in that emits a
**valid WAV** of silence via the stdlib wave module only (8 kHz mono
16-bit, duration = text length / 15 chars-per-second clamped to
0.5s..15s — synthesis_duration_ms / silence_wav are pure and
byte-stable per text). No network, no vendor SDK, no provider identifier
anywhere: real adapters register behind the Protocol in later M13
tickets and dispatch by the asset's registry alias.

oice_assets (migration  035, 0034 -> 0035, CREATE-only) is
idempotent per (org_id, voice_asset_id) (identical re-register no-op,
changed label/voice_ref/model_alias -> 409) and every asset **requires**
a caller-supplied model_alias (Model Registry alias, 1..128 chars,
never defaulted — pydantic rejects empty) plus a provider-neutral
oice_ref. Registration audits oice_assets.registered.

synthesize_speech(adapter, store, voice_asset_id, request) loads the
asset (404), confines output_path under RENDER_WORKDIR (escape ->
422), writes the WAV (OSError -> 422), and audits
oice_synthesis.completed with alias + byte count + duration. Routes
(CLIP-033 pattern): POST/GET /voice-assets, GET
/voice-assets/{voice_asset_id} (404), POST
/voice-assets/{voice_asset_id}/synthesize (Mock adapter; the injectable
TTSAdapter parameter is the future provider seam). No LLM involved.

## Image/video generation adapters (M13)

clipos_api.generation_adapters (CLIP-055) extends the CLIP-040/054
adapter discipline to media generation. ImageGenAdapter and
VideoGenAdapter are provider-neutral Protocols; both mocks take the
caller's Model Registry alias as a **provenance reference only** (no
dispatch, no provider identifier anywhere): MockImageGenAdapter
emits a valid solid-color PNG purely from stdlib struct/zlib with the
RGB derived from sha256(prompt) (byte-identical per prompt),
MockVideoGenAdapter renders a 1..30s black lavfi clip via ffmpeg
(argv-only, 60s timeout; unavailable/failing ffmpeg -> ValueError ->
422).

Persistence: image_assets (migration  036, 0035 -> 0036) — unique
(org_id, image_asset_id), content-brief gate (404), prompt <=4000,
required model_alias (never defaulted), dimensions 64..2048, and a
workdir-**relative** output_path. generate_image is idempotent: an
identical existing row returns without regenerating (brief/prompt/alias/
dimensions/path are the conflict key -> 409), output confined under
RENDER_WORKDIR (escape -> 422), audited image_assets.generated.

generate_video writes through the CLIP-038 chain instead: confined
output, then MediaIngestionStore.register_asset (kind=VIDEO,
status=READY) + 
ecord_version v1 carrying the workdir-relative
storage_ref, byte count, and sha256 checksum (store enforces
idempotency and monotonic max+1; conflict on changed title/storage_ref
-> 409), audited ideo_generations.generated. Both flows gate on the
brief and are idempotent end-to-end.

Routes (CLIP-033 pattern): POST/GET /image-generations (list 404 on
unknown brief, get 404), POST /video-generations,
GET /video-generations/{media_asset_id} (404 without asset or
version). Tests cover PNG validity via struct, prompt-color
determinism, real-ffmpeg mp4 bytes (skipif absent), full media-chain
persistence, checksum round-trips, idempotency/conflicts, and route
probes. No LLM anywhere in this ticket.

## Licensed stock/b-roll adapter interface (M13)

clipos_api.stock_adapter (CLIP-056) is the acquisition-side adapter
interface: StockClipAdapter.search(query, provider_alias, limit) returns
provider-neutral candidates (provider_ref, title, license_ref, duration,
dimensions) and MockStockClipAdapter is the deterministic stand-in
(three CC0-1.0 1920x1080 candidates derived from
sha1(provider_alias:normalized-query) — same inputs, same rows; no
network, **no vendor identifier**: providers are addressed only through
the caller-supplied registry alias provider_alias, required and never
defaulted).

Results persist into source_items (migration  037, 0036 -> 0037,
CREATE-only — the 03_DOMAIN Source table) idempotent on the natural key
(org_id, provider_alias, provider_ref): a repeat search is a no-op,
changed title/license/duration/dimensions for the same provider ref
conflict (409). Every row carries license_ref so license evidence
exists at candidate time for the M7 rights chain (the CLIP-038
IngestionGate still governs any later ingestion of the media itself —
download/ingestion is explicitly out of this interface ticket).

search_stock audits stock_clips.registered per **new** row and
stock_searches.completed per run (org, alias, query prefix, result
count). Routes (CLIP-033 pattern): POST /stock-searches (returns the
persisted items), GET /stock-items , GET /stock-items/{source_item_id}
(404). Search query 1..200 chars, limit 1..10 default 5. No LLM.

## Licensed stock/b-roll adapter interface (M13)

clipos_api.stock_adapter (CLIP-056) is the acquisition-side adapter
interface, built **on the existing M7 rights-entity chain** (no new
tables): StockClipAdapter.search(query, provider_alias, limit) returns
provider-neutral candidates and MockStockClipAdapter is the
deterministic stand-in (three CC0-1.0 1920x1080 candidates derived
from sha1(provider_alias:normalized-query) — same inputs, same rows; no
network, **no vendor identifier**: providers are addressed only through
the caller-supplied registry alias provider_alias, required and never
defaulted).

search_stock deterministically ensures the org's stock creator and
PARTNER_FEED content source — ids are **org-embedded**
({org}:stock-provider:{alias} / {org}:stock-feed:{alias}, clipped to
128) because creators/sources key on bare ids, so embedding the org
prevents cross-tenant collisions — then registers each candidate through
RightsEntitiesStore.register_item, idempotent on
(source_id, external_ref) (repeat search no-op; changed
title/license/dims/content_ref for the same provider ref -> 409). Each
item's content_ref is the neutral stock://{alias}/{provider_ref}
ref and license evidence (license_ref, title, duration, dimensions,
provider_alias) lives in item metadata for the M7 rights chain —
the CLIP-038 IngestionGate still governs any later media ingestion
(download is explicitly out of this interface ticket).

Audits: stock_searches.completed per run (org, alias, source_id, query
prefix, count) plus the rights store's own

ights_entities.creator_registered/source_registered/item_registered.
Routes (CLIP-033 pattern): POST /stock-searches (returns persisted
items), GET /stock-items?org_id=&provider_alias= (404 when the
provider source has never been searched). Search query 1..200 chars,
limit 1..10 default 5. No LLM, zero migrations — persistence reuses
content_sources/source_items from migration 0011.

## Generated-media provenance and AI labels (M13 final)

clipos_api.ai_labels (CLIP-057) closes M13 with the compliance
metadata layer. i_labels (migration  037, 0036 -> 0037, CREATE-only)
stores one disclosure row per generated asset: asset kind (reuses M7
MediaAssetKind AUDIO/IMAGE/VIDEO), workdir-relative sset_ref,
**required** generator_alias (Model Registry ref — never defaulted,
never a vendor identifier), and the label value (AI_GENERATED for all
current synthetic output; AI_ASSISTED reserved for tool-assisted
edits). Idempotent per (org_id, label_id) via deterministic ids
({org}:ai-label:{sha1[:16]}); changed asset/generator/label ->
409. Audited i_labels.recorded.


ecord_generated_media is the single registration helper: it
**validates the brief before persisting anything** (no partial state),
always registers the label, and — when a brief is present — appends a
CLIP-049 	ransformation_records row (deterministic
{org}:gen:{sha1[:16]} id, rom_ref = gen-prompt:{sha1(prompt)[:16]},
	o_ref = output ref, method = image-generation/video-generation) so
synthetic outputs enter the same 07 rights chain as editorial
transformations. Voice synthesis (not brief-linked) labels only.

**Auto-labeling at generation time**: the three generation routes
(POST /voice-assets/{id}/synthesize, POST /image-generations,
POST /video-generations) now call the helper after success — every
HTTP-mediated generation emits its AI label (and transformation for
brief-linked image/video) without any caller action. The explicit
POST /generated-media-metadata endpoint covers workflow-side or manual
registration. Routes also include GET /ai-labels (kind filter) and
GET /ai-labels/{label_id} (404). No LLM.

## Publishing adapter contract and private-test policy (M14)

clipos_api.publishing (CLIP-058) is the M14 foundation from  8_INTEGRATIONS + PRD §7.

**Adapter contract** — PublishingAdapter Protocol with health_check
(capabilities list), publish (normalized request/result), and
get_status; MockPublishingAdapter is the deterministic stand-in
(external ids derived from the idempotency key sha1 — no network, no
vendor identifier). Real platform adapters (CLIP-059/060/061) register
behind the Protocol, selected by platform slug (youtube/tiktok are generic
names, never provider credentials).

**Publishing policy** — publish_policies (migration  038, 0037 ->
0038, one per org+platform): llowed_visibilities (default PRIVATE +
UNLISTED), public_enabled (default **FALSE** — PRD: public posting
disabled by default), and kill_switch (PRD: globally disable without
redeploy). evaluate_publish_policy is the pure fail-closed evaluator:
kill switch denies everything; OBSERVE/DRAFT deny all publishing;
PRIVATE_TEST allows only PRIVATE/UNLISTED; PUBLIC also requires
public_enabled; anything outside allowed_visibilities denies; the
**callersupplied operating_mode is the future ops-config seam**
(currently request-carried; M23 binds it server-side) — but PUBLIC
blocking and kill switch enforce regardless of claimed mode.

**Submission flow** — submit_publication evaluates policy first:
DENIED attempts persist an auditable **DENIED** job (decision reason
recorded, adapter NEVER called — zero external side effect) and return it
with status: DENIED; ALLOWED attempts call the adapter once outside
any DB transaction (SQLite nested-lock safety for tests) and persist
SUCCEEDED/FAILED with the external id. Unique (org, idempotency_key)
arbitrates races at row-insert time; execute_idempotent from the M3
idempotency module is the follow-up for real network adapters (CLIP-059).

Routes (CLIP-033 pattern): policy register/get/list, publication
submit, job get (404 when unknown). Audits:
publish_policies.registered, publication_jobs.submitted,
publication_jobs.denied. No LLM.

## YouTube publishing/status adapter and platform accounts (M14)

clipos_api.platform_accounts (CLIP-059 infrastructure) persists
channel/account linkage and OAuth-scope metadata for publishing platforms
(migration  039, 0038 -> 0039, CREATE-only, unique per
(org_id, platform, account_ref), indexes on org+platform for fast
filtering). Secrets live in a vault — only references appear in this
table. POST /platform-accounts (idempotent, conflict on changed
display/channel/scopes/metadata), GET /platform-accounts/{id}
(404), GET /platform-accounts?org_id=&platform= (optional platform
filter). Audited platform_accounts.registered.

clipos_api.youtube_adapter (CLIP-059 adapter) registers
YouTubePublishingAdapter under the "youtube" platform key at
module import time via the CLIP-058 adapter-registry seam
(publishing.register_adapter). The mock adapter is a deterministic
stand-in: health_check returns ok with generic capabilities
["upload", "metadata", "status", "resumable"], publish derives
external_id = "yt-mock-{sha1(key)[:10]}" with SUCCEEDED status, and
get_status always returns SUCCEEDED — no network, no youtube-dataapi
SDK, no provider identifier. Real YouTube Data API / resumable-upload
integration replaces this adapter when OAuth credentials are
provisioned, registering under the same "youtube" key with no
adapter-resolution code changes. The publishing route now selects
the adapter per 
equest.platform via get_adapter(platform)
(falling back to Mock for any unregistered platform), so TikTok/Meta
adapters (CLIP-060/061) simply register at import time the same way.

M14 progress: CLIP-058 contract/policy + CLIP-059 YouTube adapter
done; CLIP-060 TikTok and CLIP-061 Meta remain.

## TikTok publishing/status adapter (M14)

clipos_api.tiktok_adapter (CLIP-060) registers TikTokPublishingAdapter
under the "tiktok" platform key at module import time via the CLIP-058
adapter-registry seam (publishing.register_adapter). The mock adapter is
a deterministic stand-in: health_check returns ok with generic
capabilities ["upload", "privacy", "status", "creator_info"], publish
derives external_id = "tt-mock-{sha1(key)[:10]}" with SUCCEEDED status,
and get_status always returns SUCCEEDED — no network, no
tiktok-businessapi SDK, no provider identifier. Real TikTok Content
Posting API / video-upload integration replaces this adapter when OAuth
credentials are provisioned, registering under the same "tiktok" key
with no adapter-resolution code changes. No new migration —
platform_accounts table (0039) already covers TikTok accounts.

M14 progress: CLIP-058 contract/policy + CLIP-059 YouTube adapter +
CLIP-060 TikTok adapter done; CLIP-061 Meta remains.

## Meta (Facebook/Instagram) publishing/status adapter (M14)

clipos_api.meta_adapter (CLIP-061) registers MetaPublishingAdapter
under the "meta" platform key at module import time via the CLIP-058
adapter-registry seam (publishing.register_adapter). The mock adapter is
a deterministic stand-in: health_check returns ok with generic
capabilities ["upload", "page_publish", "status", "privacy"], publish
derives external_id = "meta-mock-{sha1(key)[:10]}" with SUCCEEDED
status, and get_status always returns SUCCEEDED — no network, no
facebook-business SDK, no provider identifier. Real Meta Graph API /
video-upload integration replaces this adapter when App credentials are
provisioned, registering under the same "meta" key with no
adapter-resolution code changes. No new migration — platform_accounts
table (0039) already covers Meta accounts.

Meta developer documentation returned HTTP 400 at time of implementation
(2026-10-12). Real adapter integration requires current-doc verification
when credentials are provisioned.

M14 COMPLETE: CLIP-058 contract/policy + CLIP-059 YouTube adapter +
CLIP-060 TikTok adapter + CLIP-061 Meta adapter done. All three
platform adapters (YouTube, TikTok, Meta) registered at import with
deterministic mocks.

## Analytics provider contract (M15)

clipos_api.analytics (CLIP-062) opens M15 with the PRD §H analytics
foundation. AnalyticsAdapter is the provider-neutral Protocol
(health_check with capabilities, collect_metrics returning normalized
metrics from a published asset); MockAnalyticsAdapter is the
deterministic stand-in that returns three metrics (VIEW, LIKE, COMMENT
with value=100/10/5) — no network, no vendor SDK, no provider
identifier.

MetricKind enum covers platform-generic types: VIEW, LIKE, COMMENT,
SHARE, WATCH_TIME_MINUTES, IMPRESSION, CLICK_THROUGH. AnalyticsMetric
carries kind, value (float), unit, collected_at. AnalyticsSnapshot
bundles metrics for one content_ref from one collection run.

analytics_metrics (migration 0040, 0039 -> 0040, CREATE-only) stores
normalized measurements idempotent per (org_id, content_ref, kind,
collected_at window) — same window, same kind, same value is a no-op.
collect_analytics flow calls the adapter then persists. Routes
(CLIP-033 pattern): POST /analytics/collect, GET
/analytics/metrics?org_id=&content_ref=&kind=. Audited
analytics_metrics.collected. No LLM.

## Metric ingestion and reconciliation (M15)

clipos_api.metric_reconciliation (CLIP-063) builds on the CLIP-062
analytics contract. ingest_metrics collects via the adapter and persists
through AnalyticsStore (idempotent). reconcile_metrics compares the
latest collected metrics against the previous snapshot for the same
content_ref and records discrepancies — any metric kind where the value
changed by more than a caller-supplied threshold (default 0%). Status
is MATCH (no discrepancies), DISCREPANCY (changes found), or
NO_PREVIOUS (first collection).

reconciliation_records (migration 0041, 0040 -> 0041, CREATE-only)
stores outcomes idempotent per record_id with org+content_ref index.
Routes (CLIP-033 pattern): POST /reconciliation/run, GET
/reconciliation-records?org_id=&content_ref=. Audited
metric_reconciliation.reconciled. No LLM.

## Jev System One decision tools (ad-hoc, operator-requested)

clipos_api.jev_tools exposes TypeSafe's System One protocol as
provider-neutral decision tools: typed questions (choice over named
criteria, noul yes/no, score over ordered levels) answered from
calibrated probabilities — classification, not text generation.

JevAdapter is the Protocol (health_check, answer); MockJevAdapter is
the deterministic stand-in (per-question probabilities derive from
sha1(state|question|criteria) so identical requests always yield
identical answers — no network, no key). RemoteJevAdapter speaks
POST {JEV_BASE_URL}/v1/systemone with
`authorization: Bearer {JEV_API_KEY}` (GET /v1/models for health);
both values come from env only and are never persisted, logged, or
audited. Selection is opt-in: both env vars set -> remote, else mock
(same shape as LOCAL_ADAPTER_BASE_URL). Callers pass a registry-style
model_alias (default `systemone-latest`) — no model/vendor identifier
is hard-coded.

Routes (CLIP-033 pattern): POST /jev/answer (arbitrary typed
questions), POST /jev/triage (caller-supplied category choice plus an
urgency noul), GET /jev/health. Upstream failures normalize to 502
with bounded detail. Audited jev_tools.answered carries org, tool,
question count, model alias, and mode — never the state text or the
API key. No table, no migration, no LLM.

## Normalized performance snapshots (M15)

clipos_api.performance_snapshots (CLIP-064) rolls stored analytics
metrics (CLIP-062) into one normalized performance snapshot per
content_ref (PRD §H). build_snapshot_fields is the pure derivation:
engagement_rate = (LIKE + COMMENT + SHARE) / VIEW * 100 (0 when the
view count is missing or zero), one stored count per metric kind, all
fields defaulting to 0 when unmeasured — no LLM, no network, no
provider identifier.

performance_snapshots (migration 0042, 0041 -> 0042, CREATE-only)
persists snapshots idempotent per (org_id, content_ref): an identical
re-derivation returns the stored row, changed views/likes/comments/
shares/engagement_rate conflict (409). The flow 404s when the
content_ref has no stored analytics metrics (fail-closed — no
snapshot from absent data). Routes (CLIP-033 pattern):
POST /performance-snapshots, GET
/performance-snapshots/{snapshot_id}?org_id= (org-scoped), GET
/performance-snapshots?org_id=. Audited
performance_snapshots.recorded.

## Analytics dashboards and scheduled collection (M15)

clipos_api.analytics_dashboards (CLIP-065 part 1) is the org-level
roll-up over CLIP-064 performance snapshots: summarize_snapshots is
pure (per-kind totals, content count, average engagement rate, top-5
content by engagement with a deterministic views/content_ref
tie-break; empty input yields a zeroed summary). Route:
GET /analytics/dashboard?org_id=. No table, no LLM.

clipos_api.collection_schedules (CLIP-065 part 2) adds operator-defined
collection schedules (journey 3): collection_schedules (migration
0043, 0042 -> 0043, CREATE-only, unique (org, content_ref), unique
schedule_id, ix org+enabled) with interval_minutes 5..10080, enabled
flag, last/next run timestamps. Registration is idempotent on the
natural key; changed platform/account/external/interval/enabled
conflict (409). run_due executes enabled schedules whose next_run_at
has arrived — the clock is injected (timezone-safe Python comparison),
each due schedule collects through the CLIP-062 adapter flow and
ALWAYS advances last_run_at/next_run_at, including on adapter failure
(fail-soft FAILED outcome, never a hot loop). The scheduler tick is
caller-invoked (operator/workflow): POST /collection-schedules/run-due.
Routes: POST /collection-schedules, GET
/collection-schedules?org_id=&enabled=, POST
/collection-schedules/run-due. Audits:
collection_schedules.registered, collection_schedules.executed.

## Experiment and hypothesis data model (M16)

clipos_api.experiments (CLIP-066, M16 opener) implements the PRD §H
experiment records. experiments (migration 0044, 0043 -> 0044,
CREATE-only) are org-scoped, idempotent per (org_id, experiment_id)
with slug-validated client natural ids ([A-Za-z0-9._-]), carry a
target_metric reusing the CLIP-062 MetricKind plus a
hypothesis_summary, and start DRAFT. The status lifecycle is a pure
transition table: DRAFT -> ACTIVE -> COMPLETED -> ARCHIVED (also
DRAFT -> ARCHIVED); anything else is rejected (409), same-state is a
no-op, and every real move audits experiments.status_changed with
actor_ref/reason_ref.

hypotheses (same migration) link to one experiment — registration
requires the experiment in the same org (404 otherwise) — idempotent
per (org_id, hypothesis_id), with direction INCREASE/DECREASE/
NO_CHANGE against the experiment's target metric. Resolution is
one-shot: PROPOSED -> SUPPORTED or REJECTED, then frozen (409).
Audits: hypotheses.registered, hypotheses.status_changed.

Routes (CLIP-033 pattern): POST/GET /experiments (+?status= filter),
POST /experiments/{experiment_id}/transition, POST/GET /hypotheses
(+?experiment_id= filter), POST /hypotheses/{hypothesis_id}/transition.
No LLM, no network, no provider identifier.

## Deterministic assignment/variant orchestration (M16)

clipos_api.experiment_orchestration (CLIP-067) adds weighted variants
and reproducible subject assignment over the CLIP-066 experiments.
experiment_variants (migration 0045, 0044 -> 0045, CREATE-only,
unique (org, experiment_id, variant_key)) register only while the
experiment is DRAFT — frozen once ACTIVE (409) — with integer weights
1..100 defining bucket boundaries; idempotent with conflict on changed
label/weight. experiment_assignments (unique (org, experiment_id,
subject_ref), unique assignment_id) persist one row per subject.

select_variant is pure: bucket = sha256(org|experiment|subject) mod
sum(weights), cumulative-weight selection over variant keys sorted
lexically — same inputs, same variant, no RNG or clock. Assignment
requires the experiment ACTIVE and at least two variants (409
otherwise); an existing assignment is returned unchanged, so weight
edits and re-computations never move a stored subject. The insert
re-checks idempotency at commit time (unique index arbitrates races).

Routes (CLIP-033 pattern): POST/GET
/experiments/{id}/variants, POST/GET /experiments/{id}/assignments,
GET /experiments/{id}/variant-summary (counts per variant). Audits:
experiment_variants.registered, experiment_assignments.assigned.

## Result evaluation and business significance (M16)

clipos_api.experiment_evaluations (CLIP-068) evaluates A/B comparisons
over CLIP-067 variant samples (PRD journey 4). evaluate_significance
is pure Welch two-sample math in its large-n normal approximation:
se = sqrt(sd_a^2/n_a + sd_b^2/n_b), z = (mean_b - mean_a)/se, two-sided
p = erfc(|z|/sqrt(2)); statistical significance is |z| >= z_critical
(default 1.96 ~ 95%), business significance additionally requires
|lift_pct| >= min_effect_pct (default 5.0) — a real effect that is too
small to matter stays inconclusive. lift_pct is variant B relative to
A; equal means short-circuit to z=0/p=1; zero standard errors with a
mean difference are infinitely significant. Winner is declared only
when both gates pass (the variant ahead by the threshold), else None.
No scipy, no LLM, no network — math.erfc/math.sqrt only.

experiment_evaluations (migration 0046, 0045 -> 0046, CREATE-only,
unique (org, eval_id), ix org+experiment_id) stores every evaluation
idempotently: identical inputs replay the stored row, changed inputs
conflict (409). Routes (CLIP-033 pattern): POST
/experiments/{experiment_id}/evaluate (experiment must exist in the
org, 404), GET /experiments/{experiment_id}/evaluations. Audited
experiment_evaluations.recorded.

## Strategy-rule canary/promotion/rollback (M16)

clipos_api.strategy_rules (CLIP-069) implements learned rules with a
guarded rollout lifecycle (PRD journey 5). strategy_rules (migration
0047, 0046 -> 0047, CREATE-only, unique (org, rule_id), ix
org+status) store org-scoped rules with slug ids, a generic rule_kind
slug (never a vendor/model identifier), and a JSON parameters blob.
Registration is idempotent with conflict on changed fields (409) and
starts DRAFT.

rule_transition_allowed is the pure lifecycle table: DRAFT -> CANARY
(canary), CANARY -> ACTIVE (promote) or ROLLED_BACK (abort), ACTIVE ->
ROLLED_BACK (rollback), ROLLED_BACK -> CANARY (re-canary after a fix).
A rule can never skip canary (DRAFT -> ACTIVE is rejected 409);
same-state moves are no-ops. Every real move audits
strategy_rules.status_changed with actor_ref/reason_ref. Routes
(CLIP-033 pattern): POST /strategy-rules, GET
/strategy-rules/{rule_id} (404), GET /strategy-rules?org_id=&status=,
POST /strategy-rules/{rule_id}/transition. Audited
strategy_rules.registered. No LLM, no network.

## Revenue-source/ledger adapters (M17)

clipos_api.revenue_ledger (CLIP-070, M17 opener) implements PRD §I
monetization ingestion. RevenueAdapter is the provider-neutral
Protocol (health_check, fetch_revenue over one period);
MockRevenueAdapter is the deterministic CI stand-in returning two
entries per fetch (PLATFORM + AFFILIATE) whose external refs and
amount_cents derive from sha1(source|period|kind) — no network, no
vendor identifier. Real platform/affiliate/sponsor adapters register
behind the Protocol later.

revenue_sources (migration 0048, 0047 -> 0048, CREATE-only, unique
(org_id, source_id), ix org+platform) are org-scoped source
registrations with slug ids, platform slug, vault-only account_ref,
and the RevenueKind they report (PLATFORM/AFFILIATE/SPONSOR/PRODUCT/
LEAD_GEN); idempotent with conflict on changed fields (409).

revenue_entries is the append-only ledger: integer **amount_cents**
(never floats), 3-letter uppercase currency, (period_start,
period_end) window, unique (org_id, source_id, external_ref). sync
requires the source (404), validates the period (422), fetches through
the adapter, persists only entries not already recorded (repeat sync
of the same period imports zero and skips the rest), and audits each
insert as revenue_entries.recorded.

Routes (CLIP-033 pattern): POST /revenue-sources, GET
/revenue-sources/{source_id} (404), GET /revenue-sources?org_id=,
POST /revenue-sync, GET /revenue-entries?org_id=&source_id=. Audited
revenue_sources.registered. No LLM.

## Affiliate link/click/conversion attribution (M17)

clipos_api.affiliate_attribution (CLIP-071) implements PRD §I
affiliate tracking. Three CREATE-only tables (migration 0049, 0048 ->
0049): affiliate_links (unique (org, link_id), opaque
campaign/destination/content refs — no PII, no provider identifiers),
affiliate_clicks (unique (org, click_id), ix org+link, append-only,
link must exist 404), and affiliate_conversions (unique (org,
conversion_id), ix org+link, integer amount_cents + 3-letter currency
shared with the CLIP-070 ledger, append-only). A conversion's optional
click_ref attributes it to one click: the click must exist and belong
to the same link (404 otherwise). Every registration/record is
idempotent with conflict on changed fields (409).

summarize_attribution is pure: click/conversion counts, conversion
rate (0 without clicks), and revenue summed per currency — currencies
are never mixed. Routes (CLIP-033 pattern): POST/GET
/affiliate-links, GET /affiliate-links/{link_id}, GET
/affiliate-links/{link_id}/attribution, POST/GET /affiliate-clicks,
POST/GET /affiliate-conversions (link_id filters). Audits:
affiliate_links.registered, affiliate_clicks.recorded,
affiliate_conversions.recorded. No LLM, no network.

## Sponsorship campaign attribution (M17)

clipos_api.sponsorship (CLIP-072) implements PRD §I sponsor campaigns.
Three CREATE-only tables (migration 0050, 0049 -> 0050):
sponsor_campaigns (unique (org, campaign_id), opaque sponsor_ref, integer
contracted_cents + currency, validated date window),
sponsor_deliverables (unique (org, deliverable_id), ix org+campaign,
campaign existence enforced 404, one-shot PENDING -> DELIVERED
transition audited sponsor_deliverables.delivered), and
sponsor_attributions (unique (org, attribution_id), ix org+campaign,
integer amount_cents + currency + opaque revenue_ref, campaign
existence enforced 404, append-only, audited
sponsor_attributions.recorded). All registrations idempotent with
conflict on changed fields (409).

summarize_sponsorship is pure: delivered/total counts, delivery rate
(0 without deliverables), attributed revenue per currency (never
mixed), and variance_cents against contracted_cents when attributed
revenue exists in the contracted currency (None otherwise).

Routes (CLIP-033 pattern): POST/GET /sponsor-campaigns, GET
/sponsor-campaigns/{campaign_id} (404), POST
/sponsor-campaigns/{campaign_id}/summary, POST
/sponsor-campaigns/{campaign_id}/attributions (path/body campaign_id
must match -> 422), POST/GET /sponsor-deliverables, POST
/sponsor-deliverables/{deliverable_id}/transition. Audited
sponsor_campaigns.registered, sponsor_deliverables.registered. No
LLM, no network.

## Revenue dashboards and reconciliation (M17)

clipos_api.revenue_reconciliation (CLIP-073) closes the M17 loop over
the CLIP-070 ledger. summarize_revenue is a pure dashboard roll-up:
totals per currency (never mixed), plus per-kind and per-source totals
keyed `key:currency` so two currencies can never be added together.
GET /revenue/dashboard?org_id= optionally filters entries to an exact
(period_start, period_end) window.

reconcile_revenue is pure recorded-vs-expected math: delta_cents,
delta_pct relative to expected (zero expected reports 0% only when
recorded is also 0, else 100%), MATCH when |delta_pct| <=
tolerance_pct (default 0.0 — exact), otherwise DISCREPANCY.
run_reconciliation sums the ledger for the exact window + currency
(source 404, inverted period 422), evaluates, and persists a
revenue_reconciliations row (migration 0051, 0050 -> 0051,
CREATE-only, unique (org, reconciliation_id), ix org+source),
idempotent with conflict on changed inputs and an insert-time race
re-check; audited revenue_reconciliations.recorded.

Routes (CLIP-033 pattern): GET /revenue/dashboard, POST
/revenue-reconciliations, GET
/revenue-reconciliations?org_id=&source_id=. No LLM, no network.

## Cost ledger and provider cost ingestion (M18)

clipos_api.cost_ledger (CLIP-074, M18 opener) records provider bills
(compute, storage, egress) — distinct from `llm_cost.CostLedger`,
which records in-process model dispatch spend. CostAdapter is the
provider-neutral Protocol (health_check, fetch_costs over one period);
MockCostAdapter is the deterministic CI stand-in returning USAGE +
STORAGE entries whose external refs and amount_cents derive from
sha1(source|period|category) — no network, no vendor identifier.

cost_sources (migration 0052, 0051 -> 0052, CREATE-only, unique
(org_id, source_id), ix org+platform) are org-scoped registrations
with slug ids, platform slug, vault-only account_ref, and a generic
cost_center slug; idempotent with conflict on changed fields (409).
cost_entries is the append-only ledger: integer **amount_cents**
(never floats; positive cost, negative credit), 3-letter currency,
(period_start, period_end) window, unique (org_id, source_id,
external_ref). sync_costs requires the source (404), validates the
period (422), fetches through the adapter, persists only new entries
(repeat sync imports zero), and audits each insert as
cost_entries.recorded.

Routes (CLIP-033 pattern): POST /cost-sources, GET
/cost-sources/{source_id} (404), GET /cost-sources?org_id=, POST
/cost-sync, GET /cost-entries?org_id=&source_id=. Audited
cost_sources.registered. No LLM.

## Budgets and spend authorization (M18)

clipos_api.budgets (CLIP-075) implements PRD §I budgets as a
fail-closed gate before provider spend. budgets (migration 0053,
0052 -> 0053, CREATE-only, unique (org, budget_id), ix org+scope) cap
one generic scope_key (a cost_center value, or `org` for every
source) over an explicit (period_start, period_end) window in one
currency as integer amount_cents; inverted windows 422, changed
re-registrations 409.

authorize_spend is pure arithmetic: allowed only when spent +
requested <= cap (exact boundary allowed), remaining_after is None on
denial. The store makes currency mismatch a hard deny
(`currency_mismatch`) — a USD budget never authorizes EUR spend —
and sums already-recorded spend from CLIP-074 cost_entries for the
budget's exact window + currency, with source cost_center equal to
scope_key (scope `org` counts all sources). Every decision, allowed
or denied, persists to spend_authorizations (unique (org,
authorization_id)) and audits spend_authorizations.evaluated, so the
call is auditable; repeated ids replay or conflict (409).

Routes (CLIP-033 pattern): POST/GET /budgets, GET
/budgets/{budget_id} (404), POST /budgets/{budget_id}/authorize,
GET /budgets/{budget_id}/authorizations. Audited budgets.registered.
No LLM, no network.

## Profitability and reinvestment calculations (M18)

clipos_api.profitability (CLIP-076) computes PRD §I profit and
reserve/reinvestment splits as pure integer math. calculate_profitability
derives profit_cents = revenue - cost, margin_pct = profit/revenue
* 100 (zero revenue reports 0% when cost is also zero, else -100%),
and `profitable` (profit >= 0). calculate_reinvestment splits a
non-negative profit by reinvest_pct (0..100): reinvest_cents is the
floor of profit * pct / 100 in whole cents, reserve_cents takes the
remainder (they always sum to profit); non-positive profit yields
zeros with reason non_positive_profit, out-of-range pct raises (422
at the route).

run_report sums CLIP-070 revenue_entries and CLIP-074 cost_entries
for one exact (period_start, period_end, currency) window, computes
both figures, and persists a profitability_reports row (migration
0054, 0053 -> 0054, CREATE-only, unique (org, report_id), ix
org+period_start) — idempotent with conflict on changed inputs and an
insert-time race re-check; audited profitability_reports.recorded.

Routes (CLIP-033 pattern): POST /profitability/reports, GET
/profitability/reports?org_id=. No LLM, no network.

## Anomaly detection and finance approval gates (M18)

clipos_api.finance_controls (CLIP-077) closes M18 with two fail-closed
finance controls. detect_cost_anomaly is pure: z-score against the
population mean/stddev of a baseline (fewer than 2 points ->
INSUFFICIENT_DATA; only over-cost flags — a candidate above
mean + z_threshold * stddev is ANOMALY; zero stddev flags strictly
above-mean candidates with infinite z). run_anomaly_check scores one
CLIP-074 cost entry against the other entries of the same source +
category and persists a cost_anomalies row (migration 0055, 0054 ->
0055, CREATE-only, unique (org, anomaly_id), ix org+entry_id;
entry 404; idempotent conflict; audited cost_anomalies.recorded).

finance_approvals (same migration, unique (org, approval_id), ix
org+status) carry a requested amount through a one-shot lifecycle:
PENDING -> APPROVED or REJECTED, then frozen (illegal moves 409,
same-state no-op), with actor/reason refs; audited submitted/resolved.
evaluate_approval_gate is the pure gate — only APPROVED with matching
currency and requested <= approved allows; not_approved /
currency_mismatch / amount_exceeds_approved otherwise — and every
check audits finance_approvals.checked.

Routes (CLIP-033 pattern): POST /cost-anomalies/check, GET
/cost-anomalies?org_id=, POST/GET /finance-approvals, GET
/finance-approvals/{approval_id}, POST .../resolve, POST .../gate.
No LLM, no network.
