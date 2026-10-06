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

## CI lane matrix

The 16 lanes required by `11_CI_CD_DEPLOYMENT.md`, mapped to the current implementation:

| # | Lane | Status |
|---|------|--------|
| 1 | Formatting/lint | Implemented — `test` job: `ruff format --check` + `ruff check` |
| 2 | Python type/static checks | Implemented — `test` job: `mypy services/api/clipos_api` |
| 3 | TypeScript typecheck | Implemented — `test` job: `npx tsc --noEmit` in `apps/web` |
| 4 | Unit tests (+coverage) | Implemented — `test` job: `pytest` with `pytest-cov`, fail-under floor |
| 5 | Database migration up/down tests | Deferred — no Alembic migrations or schema yet (M2) |
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
