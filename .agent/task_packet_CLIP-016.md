# Codex Task Packet — CLIP-016

## Lease
- ticket_id: CLIP-016
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-016
- worktree: /srv/clipos/worktrees/CLIP-016
- lease_expires_at: +60 minutes from dispatch

## Objective
Implement idempotency / transactional outbox / business-key enforcement (M3) per 02 §Events ("transactional outbox initially"), 02 §9 Consistency ("database mutations and emitted domain events use transactional outbox; external writes use idempotency records"), 03 table inventory (`publishing_idempotency` exists in Publishing; foundation uses GENERIC tables), and 06's business-key formats (reuse `workflow_framework.business_key` — do NOT redefine formats):
(a) **Migration `services/api/migrations/versions/0002_outbox_idempotency.py`** (CREATE-only, downgrade intentionally not implemented — follow the 0001_foundation pattern exactly, including its docstring style):
   - `outbox_events`: id (uuid str PK), aggregate_type (str 63), aggregate_id (str 64), event_type (str 255), payload (JSON not null), business_key (str 255 null — linkage to 06 keys, INDEX not unique), status (str 16 not null, default via code: pending), attempts (int not null default 0), available_at (tz datetime not null), created_at (tz dt not null), published_at (tz dt null), last_error (text null). Indexes: ix_outbox_status_available (status, available_at), ix_outbox_business_key (business_key).
   - `idempotency_records`: key (str 255 PK), scope (str 64 not null), state (str 16 not null), request_hash (str 64 null), response (JSON null), business_key (str 255 null), attempts (int not null default 1), locked_until (tz dt null), last_error (text null), created_at, updated_at (tz dt not null). UNIQUE partial index on business_key WHERE business_key IS NOT NULL (ix_idempotency_records_business_key, unique=True, postgresql_where). Index on (state, locked_until).
(b) **`services/api/clipos_api/outbox.py`**: `OutboxEvent` + `OutboxStatus` (pending/published/failed) frozen-ish pydantic models mirroring the table; `OutboxStore` bound to a SQLAlchemy Engine/Session factory with: `enqueue(session, event)` (joins the CALLER's session/transaction — document that callers must commit together = the transactional guarantee), `enqueue_in_transaction(...)` convenience using sessionmaker BEGIN, `claim_batch(limit, lease_seconds, now)` using `SELECT ... FOR UPDATE SKIP LOCKED` returning leased rows + increments attempts, `mark_published(ids, now)`, `mark_failed(event, error, retry_backoff_seconds, now, max_attempts)` (exponential backoff via available_at; status=failed when attempts>=max_attempts, else pending), `stats()` (counts by status for operator visibility). All SQL parameterized; no string interpolation of values.
(c) **`services/api/clipos_api/idempotency.py`**: `IdempotencyRecord` model; `IdempotencyStore` with `claim(key, scope, request_hash, business_key, lock_seconds, now)` implemented as INSERT ... ON CONFLICT (key) DO NOTHING semantics (check rowcount) returning a ClaimResult enum-ish (claimed | already_succeeded(response) | in_progress(locked_until) | failed(previous)); `complete(key, response, now)`, `fail(key, error, now)`, `recover_stale(lock, now)` (in_progress + locked_until<now → reclaimable). Plus async-free sync `execute_idempotent(store, key, scope, request_hash, fn, lock_seconds)` helper: claim → run fn → complete; on already_succeeded returns stored response WITHOUT calling fn; on in_progress raises `IdempotencyInProgress` (callers retry); on failed re-claims (attempts+=1). Document crash-safety: every state transition is a single UPDATE/INSERT — no multi-step windows.
(d) **Audit/observability** (ticket scope line 3): `audit.record(...)` hooks in both modules: `outbox.enqueued`, `outbox.published`, `outbox.failed`, `idempotency.claimed`, `idempotency.completed`, `idempotency.replayed` (details carry event_type/key/scope/attempt — NEVER payload contents that could hold secrets; payload hashed or omitted).
(e) **Migration wiring**: 0001 already referenced by alembic; set `down_revision` correctly (read 0001's `revision`), script.py.mako conventions; `alembic upgrade head` must apply BOTH on a fresh database; offline SQL render must include both tables.
(f) **Tests**:
   - `services/api/tests/test_outbox.py` (unit — sqlite/:memory or mocked session where honest; FOR UPDATE SKIP LOCKED is postgres-only — put concurrency claims in the DATABASE_URL-gated test): enqueue/claim/mark roundtrip, backoff scheduling math, max_attempts → failed, stats, audit lines parseable, payload never logged in audit details.
   - `services/api/tests/test_idempotency.py` (unit): claim/complete/replay (fn NOT called on second execute_idempotent), in_progress raises, failed re-claims with attempts increment, request_hash mismatch on succeeded key → documented behavior (raise ValueError), recover_stale, audit lines.
   - `services/api/tests/test_outbox_pg.py` (gated `DATABASE_URL`, skip otherwise — runs in CI lane 5 + orchestrator): alembic upgrade head creates both tables (idempotent second run); partial unique business_key enforcement (duplicate insert raises IntegrityError, nullable ok); `claim_batch` under 2 threads returns DISJOINT sets (SKIP LOCKED proof); enqueue rollback leaves no row (transactional guarantee).
   - Update `test_migrations.py`? Only if it hardcodes an expected table list — if so extend honestly with the two new tables (check first).
(g) **Docs** `docs/development.md` — "Outbox & idempotency" section: pattern diagrams in prose, states/leases/backoff, execute_idempotent example, consistency guarantees from 02 §9, roadmap (NATS later; publishing_idempotency table lands with the publishing tickets).
(h) `.github/workflows/baseline-ci.yml` — NO changes needed (test job already runs `alembic upgrade head` + pytest with DATABASE_URL); do not remove env.

## Environment constraints
- Sandbox: NO network, NO docker. venv EXISTS (sqlalchemy/alembic/temporalio preinstalled). Self-verify: `.venv/bin/pytest -q services/api` (pg tests SKIP honestly), ruff format/check/fix, mypy, `alembic -c services/api/alembic.ini upgrade head` CANNOT run without a DB in-sandbox — verify offline: `alembic ... upgrade head --sql` renders BOTH tables (sqlite-based unit tests may use `create_all` — that's fine for unit tests; the migration itself must be exercised via the gated test).
- Existing suite baseline: 209 passed / 7 skipped — all must stay green (CLIP-015's workflow tests included; do not import worker-heavy modules in new tests unless needed).
- mypy scope `services/api/clipos_api` must stay clean; ruff too.
- `git add -A` BEFORE `bash scripts/secret_scan.sh`.

## Required reading
02_SYSTEM_ARCHITECTURE.md §Events + §9 Consistency, 03_DOMAIN_DATA_MODEL.md (publishing_idempotency + table conventions), 06_WORKFLOW_ORCHESTRATION.md (idempotency business keys — reuse workflow_framework.business_key), services/api/migrations/versions/0001_foundation.py (pattern), services/api/clipos_api/repository.py (SQL store conventions), audit.py/audit_trail.py, services/api/clipos_api/workflow_framework.py (business_key), docs/development.md, .agent/RUNNER_CONVENTIONS.md.

## Scope boundaries
- GENERIC foundation tables only — NOT the publishing-specific `publishing_idempotency` row wiring (publishing tickets), no NATS, no business workflows.
- No changes to 0001 migration; no provider identifiers; env/args only; no secrets.
- Do NOT modify CLIP-006..015 modules (adding NEW modules + migration + tests + docs only; test_migrations.py honest extension allowed if table list asserted).

## Deliverables (exact paths)
1. services/api/migrations/versions/0002_outbox_idempotency.py
2. services/api/clipos_api/outbox.py
3. services/api/clipos_api/idempotency.py
4. services/api/tests/test_outbox.py
5. services/api/tests/test_idempotency.py
6. services/api/tests/test_outbox_pg.py
7. docs/development.md section
8. .agent/dryrun/CLIP-016_completion_report.json with REAL gate totals

## Acceptance (orchestrator re-runs — incl. REAL Postgres: migration + SKIP LOCKED + partial unique + rollback)
- Sandbox: pytest green (pg SKIPs), ruff/mypy clean, offline alembic SQL shows both tables.
- Orchestrator: compose pg up → alembic upgrade head (0001+0002) idempotent → DATABASE_URL pytest green (concurrency/disjoint-claim + unique + rollback tests RUN) → full suite green → validators green (`git add -A` before scan) → teardown.
- CI 4 jobs green (lane 5 applies 0002 automatically).

## Allowed scope
paths: services/api/{migrations/versions/0002_outbox_idempotency.py,clipos_api/outbox.py,clipos_api/idempotency.py,tests/test_outbox.py,tests/test_idempotency.py,tests/test_outbox_pg.py,tests/test_migrations.py*}, docs/development.md, .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; editing 0001; NATS/event-bus code; weakening existing tests; fabricating results.

## Required gates (RUN THEM)
- .venv/bin/pytest -q services/api
- .venv/bin/ruff format services/api && .venv/bin/ruff check --fix services/api && .venv/bin/ruff check services/api && .venv/bin/ruff format --check services/api
- .venv/bin/mypy services/api/clipos_api
- python3 -m compileall services/api scripts
- cd services/api && ../../.venv/bin/alembic -c alembic.ini upgrade head --sql (offline render — BOTH tables present)  [run from worktree services/api]
- validate_handoff; validate_state; state_audit verify; architecture_guard; validate_packet .agent/task_packet_CLIP-016.md
- `git add -A` THEN bash scripts/secret_scan.sh; yaml parse baseline-ci.yml

## Completion output
Completion report + concise summary; no fabrication.
