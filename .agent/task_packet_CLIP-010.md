# Codex Task Packet — CLIP-010

## Lease
- ticket_id: CLIP-010
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-010
- worktree: /srv/clipos/worktrees/CLIP-010
- lease_expires_at: +60 minutes from dispatch

## Objective
Implement PostgreSQL migrations and the repository base (M2): SQLAlchemy 2.x declarative models for the Identity & Governance foundation tables (03: users, organizations, memberships, sessions, service_identities, audit_events), an Alembic migration setup producing ONE non-destructive initial migration (CREATE only — 07 requires policy/backup validation for destructive ops, which we do NOT do here), a repository base (engine/session factory + generic BaseRepository) with concrete Organization and Membership repositories behind the existing store Protocols, DB-gated tests (SQLite in-memory always; PostgreSQL when DATABASE_URL is set), and CI lane-5 wiring (migrations + DB tests against the compose postgres already running in the test job).

## Environment constraints
- Sandbox: NO network, NO docker. **venv EXISTS with sqlalchemy/alembic/psycopg pre-installed** — self-verify: `.venv/bin/pytest -q services/api`, `.venv/bin/ruff format services/api`, `.venv/bin/ruff check services/api`, `.venv/bin/mypy services/api/clipos_api`, `.venv/bin/alembic -c services/api/alembic.ini upgrade head --sql` (offline SQL render works without a DB!). PostgreSQL-gated tests SKIP in your sandbox (no DB) — that is correct and honest; the orchestrator runs them against real Postgres.
- NO new third-party deps beyond sqlalchemy/alembic/psycopg[binary] (already in venv — ADD them to pyproject dependencies yourself so CI installs them).

## Required reading
02_SYSTEM_ARCHITECTURE.md (stack: SQLAlchemy + Alembic + PostgreSQL pgvector), 03_DOMAIN_DATA_MODEL.md (table shapes + "constrained text/reference tables" guidance), 07_SECURITY_GOVERNANCE_RIGHTS.md (migrations policy: destructive ops need approval — your migration must be CREATE-only), 11_CI_CD_DEPLOYMENT.md (lane 5 database tests), docker-compose.dev.yml (postgres service: host port, credentials, db name for CI URL), services/api/clipos_api/{tenancy.py,identity.py,service_identities.py,audit_trail.py} (entities your tables must be able to persist), services/api/pyproject.toml, .agent/RUNNER_CONVENTIONS.md.

## Scope boundaries
- Do NOT rewrite the in-memory services — repositories implement the EXISTING store Protocols (or parallel store classes) so services can be switched later; no behavior change in CLIP-006..009 modules (import only).
- pgvector extension NOT needed for foundation tables (no embeddings yet) — plain tables.
- No HTTP routes; no seed data with real credentials.

## Deliverables (exact paths — orchestrator verifies)
1. `services/api/pyproject.toml` — add runtime deps: sqlalchemy>=2.0, alembic>=1.13, psycopg[binary]>=3.1 (choose sane floors from installed versions).
2. `services/api/alembic.ini` + `services/api/migrations/` (alembic.py env + versions/0001_foundation.py): tables users, organizations (unique slug index), memberships (unique (org_id,user_id) index), sessions (index user_id, token_hash unique-ish), service_identities (unique name), audit_events (index timestamp/action) — columns per the CLIP-006..009 contracts (ids as String/UUID-as-string, datetimes as DateTime(timezone=True), enums as String with CHECK-free plain strings for portability). Types must run on BOTH postgres and sqlite. env.py reads DATABASE_URL env (default sqlite:///.alembic-dev.db for local render ONLY if unset — document) and imports clipos_api models metadata. CREATE-only.
3. `services/api/clipos_api/repository.py` — `get_engine(url)` cached per URL, `session_scope(engine)` contextmanager (commit/rollback/close), generic `BaseRepository[T]` (add, get(id), list(limit/offset), delete(id); typed to a mapped class), concrete `OrganizationRepository` and `MembershipRepository` implementing the store Protocols from tenancy.py where feasible (if Protocol mismatch, provide `from_model`/`to_model` mappers and state clearly in docs which Protocol methods map where — prefer FULL Protocol conformance so TenancyService can be constructed with the SQL store: expose `SQLOrganizationStore(OrganizationRepository-like)` matching tenancy.StoreProtocol API — decide and document).
4. `services/api/tests/test_repository.py` — SQLite in-memory tests (always run): schema creates via metadata.create_all OR alembic against sqlite, org repo add/get/list/delete roundtrip, slug uniqueness violation surfaces as an error, membership unique (org,user), store-protocol conformance tests parametrized over InMemory vs SQL stores (same expectations both) — AT MINIMUM organizations. 
5. `services/api/tests/test_migrations.py` — `@pytest.mark.skipif(not os.environ.get("DATABASE_URL"))` PG migration test: run alembic upgrade head programmatically (alembic command API), assert expected tables exist in inspector, run upgrade AGAIN (idempotent, no error), and a downgrade/upgrade cycle if you keep reverse migration (optional; CREATE-only down_revision can be None with documented limitation). Plus an ALWAYS-RUN test rendering offline SQL (`alembic upgrade head --sql`) containing CREATE TABLE for all six tables.
6. CI `.github/workflows/baseline-ci.yml` test job: after compose up step add `DATABASE_URL: postgresql+psycopg://<creds from compose>@localhost:5433/<db>` env to the pytest step (or an explicit migrations step `alembic upgrade head` before pytest) so lane 5 activates; keep report needing all four; YAML valid. Update docs/development.md CI lane matrix line 5 (database) from Deferred to Implemented with the wiring note.
7. `docs/development.md` — "Persistence & migrations" paragraph (URL conventions, how to run migrate, which stores exist).
8. `.agent/dryrun/CLIP-010_completion_report.json` with REAL gate totals.

## Acceptance (orchestrator re-runs outside sandbox — incl. REAL Postgres)
- `.venv/bin/pytest -q services/api` green in your sandbox (PG tests skip honestly); orchestrator additionally runs with DATABASE_URL against compose postgres: migrations apply, all tests green.
- ruff format --check + check + mypy clean; compileall green.
- `alembic upgrade head` against a FRESH compose postgres creates all six tables; second run no-op.
- validate_handoff, validate_state, state_audit verify, architecture_guard, validate_packet, secret_scan, workflow YAML — green; CI 4 jobs green after merge.
- No destructive migration ops (grep your migration: no DROP/ALTER TABLE TRUNCATE).
- Existing tests (127) untouched and passing.

## Allowed scope
paths: services/api/{pyproject.toml,alembic.ini,migrations/**,clipos_api/repository.py,tests/test_repository.py,tests/test_migrations.py}, .github/workflows/baseline-ci.yml, docs/development.md, .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; DROP/TRUNCATE/ALTER-destructive ops; pgvector; routes; modifying CLIP-006..009 module behavior; fabricating results.

## Required gates (runnable here — RUN THEM)
- .venv/bin/pytest -q services/api (green; PG tests SKIP)
- .venv/bin/alembic -c services/api/alembic.ini upgrade head --sql (renders CREATE TABLEs; keep output)
- .venv/bin/ruff format services/api && .venv/bin/ruff check services/api && .venv/bin/ruff format --check services/api
- .venv/bin/mypy services/api/clipos_api
- python3 -m compileall services/api
- python3 scripts/validate_handoff.py; python3 scripts/validate_state.py; python3 scripts/state_audit.py verify; python3 scripts/architecture_guard.py; python3 scripts/validate_packet.py .agent/task_packet_CLIP-010.md
- bash scripts/secret_scan.sh; yaml parse of baseline-ci.yml
- grep -i "drop table\\|truncate\\|alter table" over your migration → must be empty

## Completion output
Completion report + concise summary; no fabrication.
