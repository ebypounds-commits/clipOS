# Codex Task Packet — CLIP-008

## Lease
- ticket_id: CLIP-008
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-008
- worktree: /srv/clipos/worktrees/CLIP-008
- lease_expires_at: +60 minutes from dispatch

## Objective
Implement RBAC/capability authorization (M1): typed permission and role contracts with BUILT-IN roles mapped to the 01 personas (Owner/Super Admin, Operator, Reviewer, Service Agent) and CLIP-007 membership roles, an AuthorizationService that enforces deny-by-default checks with tenant scoping via TenancyService.require_membership, and a CapabilityPolicy for tool-call capability checks per 07 ("RBAC plus capability checks at service boundaries", "tool calls validated against ... capability policy", GPU "exposed as capability, never assumed") — audited, tested, no routes, no SQL.

## Environment constraints
- Sandbox: NO network, NO docker; pytest/ruff/mypy re-run by the orchestrator — record SKIP+reason honestly. python3 + worktree code available; NO new third-party dependencies.

## Required reading
07_SECURITY_GOVERNANCE_RIGHTS.md (Mandatory controls; tool-call capability policy; rights gate), 01_PRODUCT_REQUIREMENTS.md (personas §3, functional domains §4, least-privilege security), 03_DOMAIN_DATA_MODEL.md (roles, permissions, role_permissions tables), 02_SYSTEM_ARCHITECTURE.md (capability abstraction: "GPU exposed as capability, never assumed"; provider-neutral aliases), services/api/clipos_api/{tenancy.py,identity.py,audit.py}, .agent/RUNNER_CONVENTIONS.md.

## Scope boundaries (neighbor tickets — DO NOT implement)
- Custom role editing / role_permissions persistence = SQL era (CLIP-010+); this ticket ships BUILT-IN roles only (frozen permission sets), documented.
- Service identity RECORDS (service_identities table) = CLIP-009; you may define a built-in role for the Service Agent persona but no identity storage.
- No HTTP routes; no SQLAlchemy; do not modify tenancy.py/identity.py/audit.py behavior (import and USE them).

## Deliverables (exact paths — orchestrator verifies)
1. `services/api/clipos_api/authorization.py` — stdlib + pydantic only:
   - `Permission` as a validated `resource:action` string type (frozen Pydantic model or NewType-style validator; format ^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$), with a small set of well-known constants grounded in 01 domains: e.g. org:read/update/delete/transfer, member:invite/remove/role_change, audit:read, settings:update, content:review/publish (rights-gate-relevant), budget:update, killswitch:activate.
   - `RoleDefinition` (name, permissions frozenset, description) + `BUILTIN_ROLES` dict: `owner` (all permissions incl org:transfer/delete, killswitch:activate), `admin` (all except org:transfer/delete + killswitch), `member` (read-ish + content:review + settings within org scope, NO destructive/admin actions), `reviewer` (reviewer persona: audit:read + content:review/publish-decision perms only), `service_agent` (least-privilege machine persona: narrow, e.g. content:review only — explicitly NOT org-admin). Document each mapping in a module docstring table.
   - `Decision` (allowed: bool, permission, reason, role_used) frozen.
   - `AuthorizationService(tenancy_service)`: `authorize(user_id, org_id, permission, *, actor_context=None) -> Decision` — deny-by-default: no active membership → deny reason "no_active_membership"; membership role not in BUILTIN_ROLES → deny "unknown_role"; permission not in role set → deny "permission_not_granted"; else allow. **Cross-tenant**: membership in another org never grants (relies on require_membership — the CLIP-007 primitive).
   - Audit: record `authz.denied` for DENIED checks of WRITE-class permissions (define write-class as any action not in {read}) — one event per denial with user/org/permission/reason; never audit allows (noise). Reuse clipos_api.audit.
   - `CapabilityPolicy(allowed: frozenset[str] = frozenset())`: `permit(tool_name: str, required: Iterable[str]) -> bool` — all required ⊆ allowed (GPU NOT in default allowed — never assumed, per 02); `audit_policy_denial(tool_name, required)` optional helper writing `capability.denied`. Constants for known capabilities: "gpu", "network_egress", "media_write", "secrets_read".
2. `services/api/tests/test_authorization.py` — pytest (no sleeps/network): permission format accepts good/rejects bad; BUILTIN_ROLES sanity (owner ⊇ admin ⊇ member sets where intended; killswitch:activate ONLY in owner; org:transfer only owner); authorize matrix table-driven (user role × permission → expected allow/deny); non-member denied; member of org-A does NOT get org-B (cross-tenant test using TenancyService fixtures); unknown membership role denied; denied write-class check emits audit event (via audit.read_events), allowed check emits none; CapabilityPolicy: empty default denies gpu; permit true only when subset; gpu NOT default-allowed asserted; determinism/idempotency (same call twice → same Decision, no state drift).
3. `docs/development.md` — "Authorization" paragraph (built-in roles table pointer, deny-by-default, capability policy, CLIP-009/010 boundaries).
4. `.agent/dryrun/CLIP-008_completion_report.json` per schema.

## Acceptance (orchestrator re-runs outside sandbox)
- pytest: prior 52 + new tests green, coverage ≥60%; ruff format --check + ruff check + mypy clean; compileall green.
- validate_handoff, validate_state, state_audit verify, architecture_guard, validate_packet (this packet), secret_scan, workflow YAML — all green.
- No new deps; tenancy.py/identity.py/audit.py behavior unchanged (git status shows only additive files + docs).
- architecture_guard provider-neutrality rule still green (no provider/model identifiers in new code).

## Allowed scope
paths: services/api/clipos_api/authorization.py, services/api/tests/test_authorization.py, docs/development.md, .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; SQL; routes; custom-role editing; modifying tenancy/identity/audit behavior; fabricating results (SKIP+reason).

## Required gates (runnable here)
- python3 -m compileall services/api
- python3 scripts/validate_handoff.py; python3 scripts/validate_state.py; python3 scripts/state_audit.py verify; python3 scripts/architecture_guard.py; python3 scripts/validate_packet.py .agent/task_packet_CLIP-008.md
- bash scripts/secret_scan.sh; yaml parse of baseline-ci.yml
- pytest expected blocked in-sandbox: SKIP+reason, never pretend.

## Completion output
Completion report + concise summary; no fabrication.
