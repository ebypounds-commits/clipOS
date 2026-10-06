# Codex Task Packet — CLIP-015

## Lease
- ticket_id: CLIP-015
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-015
- worktree: /srv/clipos/worktrees/CLIP-015
- lease_expires_at: +60 minutes from dispatch

## Objective
Implement the workflow/activity framework and conventions (M3 core) per 06_WORKFLOW_ORCHESTRATION.md:
(a) **`services/api/clipos_api/workflow_framework.py`** — the typed conventions 06 mandates:
- `WorkflowMetadata` frozen pydantic model — organization_id, brand_id/content_id (optional), policy_version, correlation_id (all non-empty; policy_version pattern like `v\d+` or semantic; validation errors), with `.as_dict()` for workflow search-attribute-friendly carrying.
- **Business keys**: `business_key(kind, **parts)` implementing 06's formats exactly — validate `kind in {publish, render, rights-review, analytics}` and required parts per kind (`publish:{account_id}:{variant_id}:{scheduled_slot}`, `render:{project_id}:{render_version}`, `rights-review:{source_item_id}:{rights_version}`, `analytics:{publication_id}:{window}`); reject missing/extra parts with ValueError; output `"{kind}:{v1}:{v2}:{v3}"`.
- **Retry categories** (06's exact list): `RetryCategory` enum {network_transient, rate_limit, auth_expired, invalid_request, policy_rejection, media_corruption, insufficient_rights} + `retry_policy_for(category) -> temporalio.common.RetryPolicy` — network_transient: exponential backoff (initial 2s, backoff coefficient 2.0, max 60s, jitter via maximum_attempts unlimited? use jitter-friendly defaults, max_attempts 10); rate_limit: longer initial 30s max_attempts 5; auth_expired: max_attempts 2 (refresh once); invalid_request / policy_rejection: max_attempts 1 (NO blind retries); media_corruption / insufficient_rights: max_attempts 1. Include `non_retryable_error_messages` where useful. Pure-logic, unit-testable without a server.
- **Durable timers**: `async def durable_wait(seconds)` documentation helper wrapping `temporalio.workflow.wait`/`workflow.sleep` semantics (MUST be called only inside workflow context — document; keep as thin documented wrapper using `from temporalio import workflow` with `workflow.sleep`), never `asyncio.sleep`.
- **Human approvals** (06's exact fields): `ApprovalRequest` frozen pydantic — action, risk, evidence (list[str] non-empty), expiry (datetime tz-aware UTC, must be future at creation), approver_role, timeout_behavior Literal["deny"] default "deny"; plus `approval_expired(request, now) -> bool`.
- `audit_workflow_event(...)` hook: writes a structured event via the EXISTING `clipos_api.audit` module (action `workflow.<name>`, actor `workflow-worker`, subject correlation_id, details) — operator-visible audit for material workflow state changes (ticket scope line 3).
(b) **`services/api/clipos_api/worker.py`** — worker bootstrap: `WorkerConfig` frozen from_env() (TEMPORAL_ADDRESS/TEMPORAL_NAMESPACE/TEMPORAL_TASK_QUEUE default `clipos-core`/max_concurrent_activities default 100; reuse TemporalConfig from temporal_config.py for address/namespace), `async def connect_client(config) -> temporalio.client.Client` (uses CLIP-014's probe patterns; no credentials stored), `def build_worker(client, config, workflows: list, activities: list) -> temporalio.worker.Worker` (Worker(..., task_queue=..., workflows=..., activities=..., max_concurrent_activities=...)), and `async def run_worker_until_stopped(...)` graceful-shutdown wrapper (KeyboardInterrupt/Cancel → worker.close()). Env-only config; no provider identifiers anywhere (registry-alias neutrality).
(c) **Reference workflow proving the framework** — `services/api/clipos_api/reference_workflow.py`: `@workflow.defn class FrameworkSelfTestWorkflow`: (1) carries/validates `WorkflowMetadata` passed as input, (2) runs activity `record_idempotent_step(business_key(...))` (activity uses a passed-in idempotency store — in-memory dict keyed by business key: returns `{"first_run": bool}` so tests prove idempotency across duplicate calls), (3) `await durable_wait(...)` (short, 1 simulated second — test uses live server with tiny sleep), (4) requests approval via `ApprovalRequest`, awaits `workflow.wait_condition` until approved flag set or expiry (default deny → returns `{"outcome": "denied_timeout"}` if never approved within wait); test drives approval by completing a second activity/future... KEEP SIMPLE: input includes `approve_after_seconds` — workflow waits min(expiry, that) then decides; returns dict with outcome, metadata correlation, idempotency first_run flag. Activities in same module (`record_idempotent_step`, `evaluate_approval`) — deterministic-friendly, idempotent, side-effect-free (memory only, documented as reference).
(d) **Tests**:
- `services/api/tests/test_workflow_framework.py` (unit, NO server): metadata validation (good/bad); business_key for all 4 kinds exact-string equality + missing/extra-part errors; retry_policy_for per category assertions (max_attempts 1 for invalid/policy/media/rights, 2 auth, ≥5 transient/rate); ApprovalRequest field validation incl expiry-past rejected + `approval_expired` true/false; audit_workflow_event writes parseable JSON line (monkeypatch audit dir like existing audit tests); durable_wait references workflow.sleep (import-level/type sanity — do NOT execute outside workflow).
- `services/api/tests/test_workflow_integration.py` (gated `TEMPORAL_ADDRESS`, skip otherwise): start Worker in-process with FrameworkSelfTestWorkflow + activities, `client.execute_workflow` with metadata + approve_after_seconds small, assert outcome `approved`, `first_run` True; run a SECOND workflow with the SAME business key input → activity returns `first_run` False (idempotency across executions documented as store-scoped — the store is per-activity-instance... to make cross-run idempotency PROVABLE, the test uses a shared dict object passed to the activity instance created once with the worker ✓ same worker process = shared store ✓). Timeout 60s.
(e) `.github/workflows/baseline-ci.yml` — ensure test job env already has TEMPORAL_ADDRESS (CLIP-014 added it; do not remove); NO other workflow changes.
(f) `docs/development.md` — "Workflow framework & conventions" section: module map, business keys, retry categories table, durable timer rule, approval default-deny, reference workflow how-to-run.

## Environment constraints
- Sandbox: NO network, NO docker; **venv EXISTS with temporalio preinstalled**. Self-verify: `.venv/bin/pytest -q services/api` (integration SKIPs honestly), ruff format/check/fix, mypy. TestClient hangs (known) — not needed; integration test uses real Client (gated, SKIPs in-sandbox).
- Keep ruff+mypy clean: workflow code needs `# type: ignore` sparingly for temporalio decorators (SDK has type support — prefer proper typing); mypy must pass.
- Existing test counts: ~192 pass baseline; all must stay passing (framework is ADDITIVE).

## Required reading
06_WORKFLOW_ORCHESTRATION.md (FULL — design principles, 12 primary workflows overview, retry categories, idempotency business-key formats, schedules, human approvals fields), 02 (Temporal topology), CLIP-014's services/api/clipos_api/{temporal_config.py,worker patterns}, audit.py + audit_trail.py (event formats), .agent/RUNNER_CONVENTIONS.md, docs/development.md.

## Scope boundaries
- Framework + ONE reference workflow only — NOT the 12 business workflows (later tickets), NO schedules (later), NO outbox/idempotency tables (CLIP-016), NO replay/failure injection (CLIP-017).
- No process-level asyncio.sleep in workflow code (ruff may not catch — self-review).
- No secrets; no provider identifiers (alias neutrality); env-only config.

## Deliverables (exact paths)
1. services/api/clipos_api/workflow_framework.py
2. services/api/clipos_api/worker.py
3. services/api/clipos_api/reference_workflow.py
4. services/api/tests/test_workflow_framework.py
5. services/api/tests/test_workflow_integration.py
6. docs/development.md section
7. .github/workflows/baseline-ci.yml untouched-or-TEMPORAL_ADDRESS-present
8. .agent/dryrun/CLIP-015_completion_report.json with REAL gate totals

## Acceptance (orchestrator re-runs — incl. LIVE workflow execution on VPS compose temporal)
- Sandbox: pytest green (integration SKIPs), ruff/mypy clean.
- Orchestrator: full suite WITH TEMPORAL_ADDRESS → integration test RUNS green against live temporal (worker spins up in-process, workflow executes, idempotency second-run proven); existing pg/storage/redis/temporal gates still pass; validators green; `git add -A` BEFORE secret_scan.
- CI 4 jobs green.

## Allowed scope
paths: services/api/{pyproject.toml,clipos_api/workflow_framework.py,clipos_api/worker.py,clipos_api/reference_workflow.py,tests/test_workflow_framework.py,tests/test_workflow_integration.py}, docs/development.md, .github/workflows/baseline-ci.yml, .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; the 12 business workflows; schedules; outbox tables; weakening tests; fabricating results.

## Required gates (RUN THEM)
- .venv/bin/pytest -q services/api
- .venv/bin/ruff format services/api && .venv/bin/ruff check --fix services/api && .venv/bin/ruff check services/api && .venv/bin/ruff format --check services/api
- .venv/bin/mypy services/api/clipos_api
- python3 -m compileall services/api scripts
- validate_handoff; validate_state; state_audit verify; architecture_guard; validate_packet .agent/task_packet_CLIP-015.md
- `git add -A` THEN bash scripts/secret_scan.sh; yaml parse baseline-ci.yml

## Completion output
Completion report + concise summary; no fabrication.
