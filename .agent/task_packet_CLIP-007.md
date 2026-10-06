# Codex Task Packet — CLIP-007

## Lease
- ticket_id: CLIP-007
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-007
- worktree: /srv/clipos/worktrees/CLIP-007
- lease_expires_at: +60 minutes from dispatch

## Objective
Implement organization tenancy and memberships (M1): typed contracts for Organization and Membership, store Protocols with thread-safe in-memory implementations (SQL is CLIP-010), and a TenancyService providing idempotent organization creation (slug key), membership management with a last-owner safety invariant, and a server-side tenant-scoping primitive (`require_membership`) — all audited through the existing audit module and covered by tests.

## Environment constraints
- Sandbox: NO network, NO docker; pytest/ruff/mypy will be re-run by the orchestrator — record SKIP+reason honestly. python3 + existing worktree code available; NO new third-party dependencies.

## Required reading
03_DOMAIN_DATA_MODEL.md (tables: organizations, memberships, users), 01_PRODUCT_REQUIREMENTS.md (§4A Control Plane; personas Owner/Operator), 07_SECURITY_GOVERNANCE_RIGHTS.md ("tenant scoping enforced server-side", "audit all privileged changes"), 09_MILESTONES_M0_M24.md, existing services/api/clipos_api/{identity.py,audit.py}, .agent/RUNNER_CONVENTIONS.md, docs/adr/0001-modular-monolith-and-worker-processes.md.

## Scope boundaries (neighbor tickets — DO NOT implement)
- CLIP-008 does RBAC/capabilities — membership roles here are a simple enum (owner/admin/member) for tenancy management ONLY; no permission/capability system, no role_permissions.
- CLIP-010 does PostgreSQL — persistence is Protocol + in-memory only.
- CLIP-006 already provides User/Session/IdentityService — do not modify identity.py (read it for style/patterns; if you need a tiny additive hook, avoid breaking its tests).
- No HTTP routes.

## Deliverables (exact paths — orchestrator verifies)
1. `services/api/clipos_api/tenancy.py` — stdlib + pydantic only:
   - Contracts: `Organization` (id, name, slug [lowercase, ^[a-z0-9][a-z0-9-]{1,62}$], state active/suspended, created_at, updated_at), `Membership` (id, org_id, user_id, role enum owner/admin/member, state active/removed, joined_at, updated_at), frozen where sensible.
   - Store Protocols + `InMemoryOrganizationStore`, `InMemoryMembershipStore` (threading.Lock; membership keyed uniquely by (org_id, user_id) keeping history by state).
   - `TenancyService(clock=None)` with: `create_organization(name, slug, idempotency)` — same slug returns the existing org unchanged (audit only on first creation: `org.created`); `get_organization`; `set_org_state(org_id, state)` (audit `org.suspended`/`org.reactivated`); `add_member(org_id, user_id, role)` — idempotent (already-active member → return existing, audit `member.added` only on first); `set_member_role` — audit `member.role_changed`; `remove_member` — state→removed, idempotent, audit `member.removed`; **last-owner invariant**: cannot `remove_member` or demote (`set_member_role`) the final ACTIVE owner of an org → raise ValueError with clear message (orgs always keep ≥1 active owner); `members_of_org(org_id, active_only=True)`, `orgs_of_user(user_id)`; `require_membership(user_id, org_id)` → returns the active Membership or raises/returns None (pick one, document it) — the server-side tenant-scoping primitive per 07; `slug_available(slug)`.
   - Audit via existing clipos_api.audit (reuse record() + read_events()).
2. `services/api/tests/test_tenancy.py` — pytest (no sleeps, injected clock): slug format rejects bad input; create idempotency (2 calls → 1 org, audit has exactly one org.created); add_member idempotency; add → set role → remove lifecycle with audit sequence; **last-owner protection** (remove final owner raises; demote final owner raises; with 2 owners, demoting one succeeds); require_membership: member gets Membership, non-member/removed-member does not (THE tenant-scoping test); orgs_of_user/members_of_org filters; suspended org behavior (document: memberships persist, service still reads — assert whatever rule you implement); concurrency: 8 threads create same slug → exactly 1 org; distinct → N; no raw tokens/credentials anywhere.
3. `docs/development.md` — add "Organization tenancy" paragraph (module, scoping primitive, CLIP-010 note).
4. `.agent/dryrun/CLIP-007_completion_report.json` per schema.

## Acceptance (orchestrator re-runs outside sandbox)
- pytest: prior 41 + new tenancy tests green, coverage ≥60% (expect ~80%); ruff format --check + ruff check + mypy clean (services/api scope); compileall green.
- validate_handoff, validate_state, state_audit verify, architecture_guard, validate_packet (this packet), secret_scan, workflow YAML — all green.
- No new deps in pyproject; identity.py tests untouched and passing.

## Allowed scope
paths: services/api/clipos_api/tenancy.py, services/api/tests/test_tenancy.py, docs/development.md, .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; SQL; routes; RBAC capability system; modifying identity.py/audit.py behavior; fabricating results (SKIP+reason).

## Required gates (runnable here)
- python3 -m compileall services/api
- python3 scripts/validate_handoff.py; python3 scripts/validate_state.py; python3 scripts/state_audit.py verify; python3 scripts/architecture_guard.py; python3 scripts/validate_packet.py .agent/task_packet_CLIP-007.md
- bash scripts/secret_scan.sh; yaml parse of baseline-ci.yml
- pytest expected blocked in-sandbox: SKIP+reason, never pretend.

## Completion output
Completion report + concise summary; no fabrication.
