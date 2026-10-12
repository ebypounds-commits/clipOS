# ExecPlan — CLIP-058 publishing adapter contract/private-test policy

## Ticket and objective
CLIP-058 (M14 opener, CRITICAL priority): provider-neutral publishing
Adapter Protocol + MockAdapter, private-test policy with PRD §7
fail-closed evaluation, publication job lifecycle with idempotency
key arbitration. No LLM, no provider identifiers, no network (Mock only).

## Current repository facts
- Head at plan time: `6361e5a` (CLIP-057 state advance, M13 closed). CI green.
- PRD §7 modes: OBSERVE/DRAFT/PRIVATE_TEST/GUARDED_PUBLIC/AUTONOMOUS_PUBLIC.
  08_INTEGRATIONS: adapter contract (health/capabilities/normalized errors/
  timeouts/rate-limits/idempotency/audit/mock/CI integration).
  CLIP-036 opportunity_decisions precedent: append-only decision log.
  workflow_framework.business_key("publish",...) idempotency format exists.
- Worktree `/srv/clipos/worktrees/CLIP-058`, branch `agent/CLIP-058`,
  venv ready, lease `mimo-clipos-m1-001` active.

## Scope / non-scope
Scope: `publishing.py`, migration 0038 (publish_policies, publication_jobs),
`tests/test_publishing.py`, main.py wiring, test_migrations, development.md.
Non-scope: real platform adapters (CLIP-059/060/061), execute_idempotent
wrap (CLIP-059 when network adapters arrive), platform_accounts registry,
publishing_idempotency table, web UI, Temporal orchestration of jobs.

## Dependencies and assumptions
- Depends on CLIP-057 (DONE). Mock side effects are null. Idempotency
  key unique (org, key) is the jobs-table arbiter — races resolve by
  unique insert at commit time (CLIP-059 adds execute_idempotent for
  real network adapters).
- operating_mode request-carried until M23 ops config.

## Files/domains expected to change
- New: `clipos_api/publishing.py`, `migrations/versions/0038_publishing.py`,
  `tests/test_publishing.py`, `.agent/plans/CLIP-058.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`, `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- DENIED attempts record decisions without calling any adapter (zero
  side effect). Adapter runs OUTSIDE DB transaction (SQLite safety).
  Audits: publish_policies.registered, publication_jobs.submitted,
  publication_jobs.denied.
- Kill switch = global off (PRD: without redeploy).

## Implementation steps
1. Contracts: visibility/operating-mode enums, AdapterHealth,
   PublishRequest/Result, PublishPolicy, PublishSubmission, PublicationJob.
2. Pure `evaluate_publish_policy` (kill switch → all denies; mode denies;
   visibility/not-enabled → denies).
3. MockPublishingAdapter (health ok capabilities, publish deterministic
   external id, get_status SUCCEEDED).
4. publish_policies + publication_jobs rows + store (register, get/list,
   get_job).
5. `submit_publication`: read-only idempotency replay + policy lookup →
   evaluate → adapter call outside txn → short write with re-check →
   audit.
6. Routes (5): policy register/get/list, publication submit, job get.
7. Migration 0038; wire main.py; test_migrations; docs.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG 0037→0038, OpenAPI probe, secret_scan,
  architecture_guard, state_audit verify.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed; validators guard state.

## Completion evidence
Ticket, branch, files, migration 0038, gates + results, suite count,
commit SHAs, CI run id.

## Remaining risks / follow-ups
- CLIP-059/060/061 wire real adapters + execute_idempotent + platform_accounts.
- M23 binds operating_mode server-side.
- Next free migration: 0039.