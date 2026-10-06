# Codex Task Packet — CLIP-017

## Lease
- ticket_id: CLIP-017
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-017
- worktree: /srv/clipos/worktrees/CLIP-017
- lease_expires_at: +60 minutes from dispatch

## Objective
Implement replay / failure-injection tests + operator visibility (M3 CRITICAL) per 09_M3 exit gates ("idempotency and failure injection tests"), 06 ("Workflow code must be deterministic and replay-safe", "Search attributes/business keys permit operator visibility"), 11 CI lane 8 ("workflow replay/idempotency tests"), and acceptance line 3 ("operator-visible audit/observability for material state changes"):

(a) **Offline replay test** — `services/api/tests/test_replay.py`:
   - A REAL history fixture already exists: `services/api/tests/fixtures/reference_workflow_history.json` (exported from a live execution of FrameworkSelfTestWorkflow — orchestrator generated; inspect its schema: {workflow_type, business_key, events:[...]}).
   - Use `temporalio.worker.Replayer` (replay_workflow) over `fixture["events"]` → assert no nondeterminism errors. Note: Replayer expects history events; convert `fixture["events"]` dicts back via `temporalio.api.history.v1.History.FromString`-style — the correct API: `temporalio.client.Client` NOT needed; `Replayer(workflows=[FrameworkSelfTestWorkflow]).replay_workflow(history)` where history = `temporalio.api.history.v1.History` — build it from the JSON with `temporalio.client.Client`? Figure the exact construction from temporalio 1.34: `from temporalio.api.history.v1 import History` then `History(message_format=...)`? The reliable route: protobuf `json_format.Parse` of the stored events list into History (google.protobuf.json_format is available via temporalio deps). Verify by RUNNING the test in-sandbox (fixture + Replayer are offline-capable — no server).
   - Also a determinism-guard test: FrameworkSelfTestWorkflow + CLIP-015 workflow module must not import `time`, `random`, `datetime.now`, `asyncio.sleep` at module scope used by workflow code (workflow.* only allowed patterns per CLIP-015 packet) — if an equivalent guard already exists in test_workflow_framework.py, reference/extend rather than duplicate (check first).
(b) **Failure-injection tests** — `services/api/tests/test_failure_injection.py` (offline unit-level, no server):
   - Outbox redelivery: claim rows (in-transaction), ABANDON without mark_published (simulate crash) → with a NEW store + expired lease (`now + lease+1s`) the same rows are claimable again (at-least-once) — use claim_batch with explicit `now` params (CLIP-016 API takes now()).
   - Delivery callback exception: simulate the relay's deliver function raising → OutboxStore.mark_failed path: attempts+1, available_at = now + exponential backoff, last_error recorded; at attempts>=max_attempts status becomes failed (terminal); audit line `outbox.failed` present with event_type + attempt, NO payload contents.
   - Idempotency crash window: claim(key) succeeds, NEVER complete → at `now + lock+1s` recover_stale/claim again returns claimable (attempts increments) → complete then replay: execute_idempotent returns stored response WITHOUT calling fn (use a counter fn).
   - Duplicate delivery: deliver the same outbox event twice to a consumer using execute_idempotent → side effect exactly once.
   - Concurrent claim (sqlite limitation: FOR UPDATE SKIP LOCKED is pg-only — keep concurrency OUT of this offline file; it lives in test_outbox_pg.py already).
(c) **Live failure-injection + search attributes** — `services/api/tests/test_workflow_failure_injection.py` (gated `TEMPORAL_ADDRESS`, skips honestly in-sandbox; orchestrator + CI run it):
   - An activity designed to FAIL on first attempt then SUCCEED (attempt counter keyed in module store or input nonce — honor idempotency conventions) → workflow completes after retry; assert attempts observed == 2 (via activity's recorded attempts in returned result), respecting CLIP-015 RetryPolicy for NETWORK_TRANSIENT.
   - Temporal SEARCH ATTRIBUTES: reference/new workflow sets a search attribute (e.g. `CustomKeywordField` = business_key or a defined keyword) at start; test lists workflows via `client.list_workflows(query=...)` or list_open filtered — assert the execution is findable BY business key (06: "Search attributes/business keys permit operator visibility"). If the SDK 1.34 typing for search attributes is strict, use `temporalio.common.SearchAttributeKey` correctly (typed API — do not use deprecated dict form).
   - Both tests must clean up (terminate executions in finally).
(d) **Operator visibility** — `services/api/clipos_api/operator.py` + route:
   - `OperatorSnapshot` frozen pydantic: outbox counts by status + oldest_pending_age_seconds, idempotency counts by state + stale_locks, last N audit events (event_type, at, outcome — tail query), temporal reachability flag (caller passes), generated_at.
   - `OperatorSnapshot.build(engine_or_session, now, last_n=20)` — pure SQL/ORM reads (parameterized), no network calls (temporal flag is a parameter, not probed inside).
   - FastAPI route `GET /operator/health` in `services/api/clipos_api/main.py` returning the snapshot (path constants in clipos_api.paths if it exists — CHECK main.py's conventions first and follow them; route-table wiring test must be extended honestly in the file that asserts routes (find it — likely test_main or similar; CLIP-008 added a route-table test).
   - Audit hook: none needed for a read-only snapshot (material changes already audited by CLIP-016) — but DO audit nothing here; document that reads are unlogged by design.
(e) **Docs** — `docs/development.md`: "Failure injection & replay" subsection (what is injected, where the tests live, CI lane 8 mapping) + "Operator visibility" runbook (endpoint, fields, how to find executions by business key).
(f) `.github/workflows/baseline-ci.yml` — the test job must RUN `test_workflow_failure_injection.py` too: it already runs pytest with TEMPORAL_ADDRESS (check the env is passed to the pytest step; CLIP-014/015 wired it — verify and only touch if actually missing).

## Environment constraints
- Sandbox: NO network/NO docker. venv EXISTS. The fixture replay test + failure-injection unit tests MUST pass in-sandbox (offline) — this is your primary self-verify. TEMPORAL_ADDRESS tests SKIP honestly.
- Baseline suite: 209 passed / 9 skipped (CLIP-016) — all stay green.
- Replayer API: you are NOT certain of the exact History construction — introspect temporalio 1.34 in the venv (`python -c "import temporalio.worker, temporalio.api.history.v1"`, help(), dir()) and VERIFY by running your test. Do not guess; do not fabricate.
- ruff/mypy clean; `git add -A` before `bash scripts/secret_scan.sh` (fixture JSON must be scan-clean — it contains only business keys/ids, no secrets).
- OutboxStore/IdempotencyStore APIs take explicit `now` — use them for lease/clock simulation; do NOT monkeypatch global time.

## Required reading
09_MILESTONES_M0_M24.md M3 exit gates, 06_WORKFLOW_ORCHESTRATION.md (replay-safe, search attributes/operator visibility), 11_CI_CD_DEPLOYMENT.md lane 8, services/api/tests/fixtures/reference_workflow_history.json (schema), services/api/tests/test_workflow_integration.py (worker/client patterns), services/api/clipos_api/{outbox.py,idempotency.py,workflow_framework.py,reference_workflow.py,main.py}, docs/development.md, .agent/RUNNER_CONVENTIONS.md.

## Deliverables (exact paths)
1. services/api/tests/test_replay.py
2. services/api/tests/test_failure_injection.py
3. services/api/tests/test_workflow_failure_injection.py
4. services/api/clipos_api/operator.py
5. main.py route + route-table test wiring (paths file if conventions require)
6. docs/development.md sections
7. .agent/dryrun/CLIP-017_completion_report.json with REAL gate totals

## Acceptance (orchestrator re-runs — incl. LIVE temporal: failure-injection retries + search-attribute listing + replay over live-fetched history)
- Sandbox: offline replay + failure-injection + full suite green (temporal tests SKIP).
- Orchestrator: stack up → full env suite (live failure-injection + search attrs RUN) → live history export + Replayer over FRESH history → validators (`git add -A` before scan) → teardown → land.
- CI 4 jobs green (test job runs the new gated tests via TEMPORAL_ADDRESS).

## Allowed scope
paths: services/api/{clipos_api/operator.py,clipos_api/main.py,clipos_api/paths.py,tests/test_replay.py,tests/test_failure_injection.py,tests/test_workflow_failure_injection.py,tests/*route*}, docs/development.md, .github/workflows/baseline-ci.yml (only if env wiring actually missing), .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; modifying CLIP-016 modules (outbox/idempotency logic — READ only; extend via new operator.py); editing the fixture; weakening tests; fabricating results.

## Required gates (RUN THEM)
- .venv/bin/pytest -q services/api
- .venv/bin/pytest -q services/api/tests/test_replay.py -v (must RUN, not skip)
- ruff format --check + check + mypy + compileall
- validate_handoff; validate_state; state_audit verify; architecture_guard; validate_packet
- `git add -A` THEN bash scripts/secret_scan.sh; yaml parse baseline-ci.yml

## Completion output
Completion report + concise summary; no fabrication.
