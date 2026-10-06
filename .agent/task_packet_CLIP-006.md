# Codex Task Packet — CLIP-006

## Lease
- ticket_id: CLIP-006
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-006
- worktree: /srv/clipos/worktrees/CLIP-006
- lease_expires_at: +60 minutes from dispatch

## Objective
Implement the identity/session foundation (M1): typed Pydantic contracts for users and sessions, a store abstraction (Protocol) with an in-memory implementation (PostgreSQL persistence is a LATER ticket CLIP-010 — do NOT add SQLAlchemy/alembic now), and an IdentityService implementing session create (idempotent), rotation, revoke, expiry, CSRF token generation, and secure cookie attribute helpers — all covered by tests and writing operator-visible audit events through the existing audit module.

## Environment constraints
- Sandbox: NO network, NO docker — pip/npm gates are SKIP+reason (orchestrator re-runs outside). python3 + the worktree's existing code are available (no new third-party deps: pydantic and fastapi are already installed in the orchestrator's verification venv — but you cannot run pytest in-sandbox; SKIP honestly).
- OCCUPIED ports: 22 53 80 443 3000 3080 3100 5432 6001 6002 8000 8080 8090 8642 8776 8888 9119 9377 11235 18080.

## Required reading
03_DOMAIN_DATA_MODEL.md (Identity & Governance tables: users, sessions, organizations...), 01_PRODUCT_REQUIREMENTS.md (Control Plane §4A, Security), 07_SECURITY_GOVERNANCE_RIGHTS.md (secure cookies/session rotation/CSRF, tenant scoping, audit privileged changes), 09_MILESTONES_M0_M24.md (M1 scope), existing services/api/clipos_api/{audit.py,modules.py,registry.py,main.py}, .agent/RUNNER_CONVENTIONS.md, .agent/task_packet_CLIP-005.md (packet format), docs/adr/0001-modular-monolith-and-worker-processes.md.

## Scope boundaries (neighbor tickets — DO NOT implement)
- CLIP-007 will do organizations/tenancy/memberships — you may TYPE a minimal org_ref on contracts but no membership logic.
- CLIP-008 does RBAC/authorization — no permission checks beyond simple ownership/existence.
- CLIP-009 does service identities + immutable audit trail — just USE audit.py for your events.
- CLIP-010 does PostgreSQL migrations + repositories — persistence here is a Protocol + in-memory impl only.
- No HTTP routes this ticket (later tickets wire FastAPI endpoints).

## Deliverables (exact paths — orchestrator verifies)
1. `services/api/clipos_api/identity.py` — single new module (stdlib + pydantic only), containing:
   - Pydantic v2 contracts: `User` (id, email/subject, display_name, status enum active/disabled, created_at, updated_at, extra: org_ref optional), `Session` (id, user_id, token_hash (never store raw tokens), csrf_token, state enum active/expired/revoked, created_at, last_seen_at, expires_at, rotated_from optional, idempotency_key optional), frozen/immutable where sensible.
   - `SessionStore` Protocol: get, put_if_absent (returns existing on key collision), update, delete/list_by_user.
   - `InMemorySessionStore`: thread-safe (threading.Lock), idempotent `put_if_absent` keyed by idempotency_key.
   - `IdentityService`: `create_session(user_id, ttl, idempotency_key=None)` → stores session, returns (session, raw_token, csrf) where raw_token shown ONCE (store keeps only hash via hashlib.sha256), calling audit for `session.created`; `rotate_session(raw_token)` → validates, revokes old state, creates successor with rotated_from set (audit `session.rotated`); `revoke_session(raw_token)` (audit `session.revoked`, idempotent — revoking an already-revoked session succeeds as no-op); `touch(raw_token)` updates last_seen_at; `get_session(raw_token)` returns None for unknown/expired/revoked; expiry decided by an INJECTED clock (`clock: Callable[[], datetime]` ctor param, default now UTC) so tests never sleep.
   - `new_csrf_token()` (secrets.token_urlsafe), `cookie_attributes(secure=True)` dict → {httpOnly, secure, sameSite:"Lax"} per 07.
   - Audit calls go through the EXISTING clipos_api.audit module (inspect its API and reuse it; if its signature needs a tiny additive extension, do it without breaking existing tests).
2. `services/api/tests/test_identity.py` — pytest (no network, no sleep): contract validation (bad email/state rejected), create→get roundtrip, idempotent create with same key returns SAME session (single store entry), token hash != raw token + raw token not recoverable from session, rotation invalidates old token and successor links rotated_from, revoke idempotent, expiry via injected clock (advance clock, session gone), touch updates last_seen_at, csrf tokens unique across 100 generations, cookie_attributes contains httpOnly/secure/sameSite=Lax, audit events recorded for create/rotate/revoke (assert via audit module's read surface — inspect how audit exposes events; if only append exists, add a read helper in audit.py additively), and a small concurrency test (8 threads creating with distinct keys → 8 sessions; same key concurrently → exactly 1).
3. Update `docs/development.md` with a one-paragraph "Identity/session foundation" note (module, store protocol, CLIP-010 will provide the SQL store).
4. `.agent/dryrun/CLIP-006_completion_report.json` per schema (record pytest as SKIP+reason in-sandbox).

## Acceptance (orchestrator re-runs outside sandbox)
- pytest: all prior 28 tests + new identity tests green; coverage gate still ≥60%; ruff format --check + ruff check + mypy (services/api scope) clean; compileall green.
- No new third-party dependencies in pyproject (pydantic/fastapi already present).
- validate_handoff, validate_state, state_audit verify, architecture_guard, validate_packet (this packet), secret_scan, workflow YAML — all green; CI 4 jobs.
- Existing audit/registry/providers tests untouched and passing (no behavior changes outside additive audit read helper if needed).
- No credentials, no raw-token logging anywhere (grep your module for print/log of raw_token — must be none).

## Allowed scope
paths: services/api/clipos_api/identity.py, services/api/clipos_api/audit.py (additive read helper only), services/api/tests/test_identity.py, docs/development.md, .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; SQL/SQLAlchemy/alembic; HTTP routes; RBAC logic; fabricating results (SKIP+reason).

## Required gates (runnable here)
- python3 -m compileall services/api
- python3 scripts/validate_handoff.py; python3 scripts/validate_state.py; python3 scripts/state_audit.py verify; python3 scripts/architecture_guard.py; python3 scripts/validate_packet.py .agent/task_packet_CLIP-006.md
- bash scripts/secret_scan.sh; yaml parse of .github/workflows/baseline-ci.yml
- python3 -m pytest -q services/api — expected to FAIL with import errors (no deps in sandbox): record as SKIP+reason, do not pretend it passed.

## Completion output
Completion report + concise summary; no fabrication.
