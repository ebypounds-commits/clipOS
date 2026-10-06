# Codex Task Packet — CLIP-009

## Lease
- ticket_id: CLIP-009
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-009
- worktree: /srv/clipos/worktrees/CLIP-009
- lease_expires_at: +60 minutes from dispatch

## Objective
Implement service identities and the immutable audit trail (M1): (a) typed ServiceIdentity contracts with credential REFERENCES only (03: `api_credentials` references only; 07: secrets manager references only — never store raw secrets) plus issue-once hashed service tokens and least-privilege role wiring to CLIP-008's `service_agent` built-in role; (b) an append-only, hash-chained `AuditTrail` where every event embeds seq + prev_hash + sha256 of its canonical payload, with chain verification that detects modification, deletion, and reordering — the tamper-evident "immutable" foundation for the `audit_events` table (SQL lands in CLIP-010).

## Environment constraints
- Sandbox: NO network, NO docker; NO new third-party dependencies.
- **A working venv EXISTS in this worktree — self-verify everything before finishing:**
  - `.venv/bin/pytest -q services/api`
  - `.venv/bin/ruff format services/api` then `.venv/bin/ruff check services/api`
  - `.venv/bin/mypy services/api/clipos_api`
  - Iterate until ALL are green. Do not report done on red.

## Required reading
03_DOMAIN_DATA_MODEL.md (service_identities, audit_events, api_credentials references-only), 07_SECURITY_GOVERNANCE_RIGHTS.md (secrets manager references only; audit all privileged changes; least privilege), 01_PRODUCT_REQUIREMENTS.md (Service Agent persona), services/api/clipos_api/{audit.py,authorization.py (service_agent role),identity.py (token-hash pattern),tenancy.py}, .agent/RUNNER_CONVENTIONS.md, .agent/task_packet_CLIP-008.md.

## Scope boundaries (neighbor tickets — DO NOT implement)
- CLIP-010: PostgreSQL + migrations — stores here are Protocol + in-memory only.
- Do NOT modify audit.py/identity.py/tenancy.py/authorization.py behavior (import & reuse; additive-only if unavoidable).
- No HTTP routes; no storing raw secrets/tokens (hash-once pattern like identity.py; credential_ref strings only).
- CLIP-008 already defines the `service_agent` built-in role — reference it, do not duplicate role definitions.

## Deliverables (exact paths — orchestrator verifies)
1. `services/api/clipos_api/service_identities.py` — stdlib + pydantic:
   - `ServiceIdentity` (frozen): id, name (unique slug-ish), org_id (optional str), kind enum (service_agent/integration/worker), state enum active/disabled, credential_ref (str | None — a REFERENCE like "vault:prod/<name>", never a secret), token_hash (str | None, sha256), created_at, updated_at, disabled_at optional.
   - `ServiceIdentityStore` Protocol + `InMemoryServiceIdentityStore` (threading.Lock, unique by name).
   - `ServiceIdentityService(clock=None)`: `create(name, kind, org_id=None, credential_ref=None)` → idempotent by name (audit `service_identity.created` only first time), returns (identity, raw_token) ONCE (store keeps hash only); `issue_token(name)` rotates token (old invalid; audit `service_identity.token_rotated`); `disable(name)`/`enable(name)` (audit events, idempotent); `authenticate(raw_token) -> ServiceIdentity | None` (None for unknown/disabled/rotated-away); `get(name)`, `list(org_id=None)`.
   - All events through clipos_api.audit.
2. `services/api/clipos_api/audit_trail.py` — stdlib + pydantic:
   - `AuditEvent` (frozen): seq (int ≥1), timestamp, action, actor, subject, details (dict), prev_hash (str, "" for genesis), event_hash (str).
   - `AuditTrail(store)`: `append(action, actor, subject, details)` → computes event_hash = sha256(f"{seq}|{timestamp}|{action}|{actor}|{subject}|{canonical_json(details)}|{prev_hash}") with canonical_json = json.dumps(sort_keys,separators); thread-safe; returns event. `verify() -> list[str]` → [] when intact, else human-readable problems ("seq gap at 3", "hash mismatch at 4", "prev_hash mismatch at 5", "detached event at 6"). `events(action=None, actor=None)` filters.
   - `AuditTrailStore` Protocol + `InMemoryAuditTrailStore` (private list, NO update/delete API exposed — immutability by interface).
   - Convenience: `record_from_legacy(legacy_event_dict)` to import audit.py records (bridge for later migration) — optional but preferred.
3. `services/api/tests/test_service_identities.py` — create idempotency (one created event), token issue-once (raw returned once, store has hash not raw — assert raw not in any stored field), rotation invalidates old token, disable blocks authenticate + idempotent, enable restores, authenticate unknown → None, credential_ref stored verbatim but NEVER a secret-looking value validated (assert no field equals the raw token), concurrency same-name create → 1 identity, audit sequence assertions via audit.read_events.
4. `services/api/tests/test_audit_trail.py` — genesis + append + verify=[] ; tamper MODIFY an event's details → verify reports hash mismatch ; DELETE middle event → seq gap/detached ; REORDER two events → prev_hash mismatch ; duplicate seq rejected ; filters work ; thread-safety: 8 threads × 25 appends → 200 events, verify=[] , seqs exactly 1..200 ; interface immutability: store exposes no mutation method (assert not hasattr for update/delete/reverse/pop on the public store API... pick concrete assertions: hasattr(store, "update") is False etc.) ; canonical/deterministic hash (same inputs → same hash).
5. `docs/development.md` — "Service identities & immutable audit" paragraph (hash chain design, credential-ref policy, CLIP-010 SQL note).
6. `.agent/dryrun/CLIP-009_completion_report.json` per schema with REAL pytest/ruff/mypy totals from your venv runs.

## Acceptance (orchestrator re-runs outside sandbox)
- pytest: prior 98 + new tests green, coverage ≥60%; ruff format --check + ruff check + mypy clean; compileall green.
- validate_handoff, validate_state, state_audit verify, architecture_guard, validate_packet (this packet), secret_scan, workflow YAML — all green.
- No new deps; existing modules behavior unchanged (git status shows only new files + docs).
- No raw secret/token persisted anywhere (grep your modules: raw_token only appears as function args/returns).

## Allowed scope
paths: services/api/clipos_api/service_identities.py, services/api/clipos_api/audit_trail.py, services/api/tests/test_service_identities.py, services/api/tests/test_audit_trail.py, docs/development.md, .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; SQL; routes; modifying existing module behavior; storing raw secrets; fabricating results.

## Required gates (runnable here — RUN THEM)
- .venv/bin/pytest -q services/api (must end green)
- .venv/bin/ruff format services/api && .venv/bin/ruff check services/api && .venv/bin/ruff format --check services/api
- .venv/bin/mypy services/api/clipos_api
- python3 -m compileall services/api
- python3 scripts/validate_handoff.py; python3 scripts/validate_state.py; python3 scripts/state_audit.py verify; python3 scripts/architecture_guard.py; python3 scripts/validate_packet.py .agent/task_packet_CLIP-009.md
- bash scripts/secret_scan.sh; yaml parse of baseline-ci.yml

## Completion output
Completion report + concise summary; no fabrication.
