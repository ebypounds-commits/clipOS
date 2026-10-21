# ClipOS — Continuation Build Plan

**Date**: 2026-10-21
**Authoritative repository**: `/srv/clipos/clipOS` (VPS). The Windows path `C:\Users\Gozmar Dynamics\XiaomiMiMoProjects\Clipos` is a **scratch mirror, not a git checkout** — all work happens on the VPS.
**Baseline**: `main` @ `4e9570c` (docs: session handoff note for P0-1 continuation)
**In-flight branch**: `fix/p0-1-rights-enforcement` @ `4e0b049` (worktree `/srv/clipos/worktrees/P0-1-RIGHTS`, clean tree, lease `P0-1-RIGHTS`)
**Method**: read-only-first verification. No product code was changed, no test weakened, no state rewritten. This plan is the deliverable; the implementation work it describes has not been started.

---

## 0. Executive summary

Three verified facts decide what happens next.

**1. P0-1 is implemented but not landable.** The rights gate is now consulted on the publication path. The branch's own 13 enforcement tests pass against the branch's own code. But **16 tests still fail**: 10 route-level publication tests that publish without a rights record, and 6 tests of the private-mode qualification harness. The failures are not in the enforcement logic — they are the *pre-existing* tests and harnesses that assumed publication was unconditional.

**2. The 24-vs-16 failure discrepancy had a single mechanical cause, and it invalidates the branch's evidence.** The shared virtualenv `/srv/clipos/e2e-venv` holds an **editable install of `clipos_api` mapped to the main checkout** (`/srv/clipos/clipOS/services/api`). A test run inside a worktree therefore imported `main`'s `clipos_api`, not the branch's. Every "green" result recorded in `HANDOFF.md` §3 was produced against the wrong code. The defect is proven in §2 and is itself a P0 item: it is the same class as the defect the audit found — a control that reports success without observing the thing it claims to control.

**3. Three qualification controls are structurally incapable of failing.** `private_mode_qualification.py` P4 compares a publication status against the literal `"PUBLISHED"`, which the API never returns, so it has always passed — including while the rights gate was unenforced. P1/P2/P3 assert only `DENIED`, so once rights is checked first they pass for the wrong reason. P5 now fails for the wrong reason. Land P0-1 without repairing these and the next audit finds the same class of hole.

Sequence: **W0** (test-evidence integrity) → **W1** (finish P0-1) → **W2** (adapter provenance) → **W3** (cross-tenant isolation) → **W4** (wire the proven gates into CI) → **W5** (first production month) → **W6** (hardening). W0 and W1 are both blocking; W2/W3 are independent of each other and can run in parallel with W1's test repair.

---

## 1. Verified current state

Every row was observed on the VPS on 2026-10-21. Commands are copy-pasteable and are the canonical ones for this repository (see §7 for the corrected invocation).

| # | Check | Result | Verdict |
|---|---|---|---|
| 1 | `git log` / `git status` on `main` | `4e9570c`, tree clean, `main` = `be4acd2` + audit reports + handoff note | healthy |
| 2 | `git worktree list` | `/srv/clipos/clipOS` (`main`), `/srv/clipos/worktrees/P0-1-RIGHTS` (`fix/p0-1-rights-enforcement` @ `4e0b049`) | as documented |
| 3 | P0-1 diff vs `main` | 4 files, +633/−44: `publishing.py` (+59), `security_qualification.py` (+123), `test_publication_rights_enforcement.py` (+329, new), `test_publishing.py` (+166) | scoped correctly, nothing else touched |
| 4 | P0-1 enforcement tests vs **branch code** | 13/13 pass | implementation is sound |
| 5 | `test_publishing.py` vs **branch code** | 39/39 pass (incl. 3 strengthened reason assertions) | sound |
| 6 | `test_security_qualification.py` vs **branch code** | pass — C5 now asserts a *real publication* is `DENIED` with a rights reason | control fixed |
| 7 | 8 publishing-related test files vs **branch code** | **16 failed, 105 passed** (29.3 s) | **P0-1 incomplete** |
| 8 | Full suite, **main's** code, branch's tests | 24 failed, 1826 passed, 1 skipped (203 s, 93.44 % cov) | misleading — wrong code under test (§2) |
| 9 | Full suite, **branch's** code | **16 failed, 1834 passed, 1 skipped**, 94.03 % cov, 242 s | **true baseline for W1** |
| 10 | Infrastructure | postgres/redis/minio/temporal all healthy on the dev stack; Python 3.14.4; venv `/srv/clipos/e2e-venv` | available |
| 11 | DB state | `rights_records`, `rights_events`, `provenance_manifests`, `source_items`, `creators` all present in `clipos_dev` | no migration needed for P0-1 |
| 12 | P0-2 (`adapter_kind` in responses) | zero occurrences anywhere in `clipos_api/` | genuinely unimplemented |
| 13 | Lease | `P0-1-RIGHTS.lock` — owner `mimo-clipos-p01`, `expires_at 2026-10-20T19:57Z`, state `active` | **expired** — renew before further work |
| 14 | `BUILD_STATE.json` | `current_ticket=NONE`, `status=M24_COMPLETE`, 108 completed | terminal state, coherent |
| 15 | `release/v1.manifest.json` | pins `tickets_total: 108` and the six qualification harnesses | a new ticket changes the freeze (see §6) |

### 1.1 The 16 real failures (branch code, exact)

True baseline, branch `4e0b049` with the branch's code actually imported:

```
16 failed, 1834 passed, 1 skipped, 887 warnings in 242.19s (0:04:02)   coverage 94.03%
```

Arithmetic against the audit's `main` baseline (1838 passed, 0 failed, 0 skipped, 94.05 %): 13 new tests added, 16 previously-passing tests now fail. The single skip is `tests/test_dependency_security.py:129` — "git not available" — an artifact of the `sudo -u clipos -H env …` invocation dropping `git` from `PATH`, not a code skip. Coverage is unchanged to within rounding: the branch adds a small amount of covered code and no uncovered code.

| File | Failures | Cause |
|---|---|---|
| `tests/test_public_publishing_gate.py` | 5 (`:366 :383 :414 :442 :473`) | route publishes `storage://content` with no rights record → `DENIED`; two tests assert `decision_reason == "owner_approval_required"` and now get `rights_not_clear:not_found` (rights is evaluated first) |
| `tests/test_tiktok_adapter.py` | 3 (`:165 :196 :255`) | 2 direct `submit_publication(...)` calls now default to `rights_clear=False` → `DENIED`; 1 route test publishes without rights |
| `tests/test_meta_adapter.py` | 2 (`:171 :230`) | same shape as the TikTok cases |
| `tests/test_private_mode_qualification.py` | 6 (`:107 :127 :205` + 3 `leaky` wrappers at `:219 :242 :265`) | 3 harness tests fail because the qualification walk now denies the publication; 3 monkeypatched `leaky` wrappers have the pre-change signature `(policy, visibility, operating_mode, account_approved=False)` and raise `TypeError` when the route calls with 5 positional args |

`test_publication_rights_enforcement.py`, `test_publishing.py`, `test_security_qualification.py`, `test_threat_model.py` — **0 failures**.

---

## 2. New finding — the test-evidence integrity defect (W0)

**Mechanism.** `/srv/clipos/e2e-venv` was created with `pip install -e services/api` **from the main checkout**. The generated finder file states it explicitly:

```
MAPPING: dict[str, str] = {'clipos_api': '/srv/clipos/clipOS/services/api/clipos_api'}
```

`install()` appends that finder to `sys.meta_path`, so it resolves `clipos_api` to the main tree unless the package is found earlier on `sys.path`. Pytest inserts `services/api/tests` (the test package root), **not** `services/api`, so nothing shadows the editable mapping. A test run inside a worktree therefore executes `main`'s product code against the branch's test files.

**Proof** (probe run in the worktree):

```
$ cd /srv/clipos/worktrees/P0-1-RIGHTS && python /tmp/probe_import.py
clipos_api file = /srv/clipos/clipOS/services/api/clipos_api/__init__.py
has _rights_cleared = False

$ PYTHONPATH=/srv/clipos/worktrees/P0-1-RIGHTS/services/api python /tmp/probe_import.py
clipos_api file = /srv/clipos/worktrees/P0-1-RIGHTS/services/api/clipos_api/__init__.py
has _rights_cleared = True
```

**Blast radius.** The failure signature is unmistakable in the run that used the wrong code: `AttributeError: <module 'clipos_api.publishing' from '/srv/clipos/clipOS/services/api/clipos_api/publishing.py'> has no attribute '_rights_cleared'`, and the coverage report attributed every statement to `/srv/clipos/clipOS/...`. Consequently:

- `HANDOFF.md` §3 items 1–4 ("39/39 pass", "13 passing", "C5 verified green") were **not** produced against the branch's code and carry no evidentiary weight.
- The 24-failure run and the 16-failure run are the *same* branch; the difference is only which code was imported.
- Any future worktree test run on this host is invalid unless the import path is pinned. This is a live trap for every subsequent work package, not a one-off.

**Why this is P0, not a nit.** The audit's central finding was "a correct component whose control observed the component instead of the path". This defect is the same shape one level up: a *test run* that reports on the wrong artifact. It is cheap to fix and it invalidates every claim made without it.

---

## 3. W0 — Test-evidence integrity (blocks all work)

**Goal**: a test run in this repository can never again silently exercise a different checkout's code, and the run procedure is written down where a runner will read it.

| Field | Detail |
|---|---|
| **Priority** | P0 — prerequisite for W1 and every later verification |
| **Dependency** | none |
| **Scope** | `services/api/tests/conftest.py` (new), `.agent/RUNNER_CONVENTIONS.md`, `.agent/PLANS.md` (optional), `docs/development.md` |
| **Implementation** | (a) Add `services/api/tests/conftest.py` with a session-scoped autouse guard: resolve `clipos_api.__file__` and assert it is under the `services/api` directory that owns the running test tree; on mismatch, fail with a message naming both paths and the fix (`PYTHONPATH=<repo>/services/api` or reinstall the editable package in the worktree). (b) Document the canonical worktree test command in `.agent/RUNNER_CONVENTIONS.md` under the "Ground-truth rule" section, because that section is what makes a result evidence. (c) Document it in `docs/development.md` for humans. |
| **Acceptance criteria** | 1. With `PYTHONPATH` unset, the suite **fails immediately** with a clear path-mismatch error instead of running against `main`. 2. With `PYTHONPATH=<worktree>/services/api`, the suite runs the worktree code. 3. The procedure is documented in the runner conventions. |
| **Tests** | The guard itself (a unit test that points the check at a foreign path and asserts it raises) plus the full suite in both modes. |
| **Risk** | Low. The guard is a no-op in CI (`pip install -e services/api` from the repo root resolves to the same tree) — but it must be verified in CI, not assumed. |

**Operator note.** A per-worktree virtualenv (`python -m venv /srv/clipos/venvs/<ticket>`) is the durable alternative to `PYTHONPATH`; the guard above is required either way because it is the only thing that fails *loudly*.

---

## 4. W1 — Finish P0-1: enforce the rights gate on the publication path

**Goal**: `rights_status != CLEAR → publication blocked`, enforced, tested, and reported by controls that can actually fail.

**Status**: implementation landed (`4e0b049`); 16 failures remain; branch not landable.

### W1.0 — Decide the denial-reason semantics (owner call, 15 minutes)

The branch evaluates rights **first**, so every denial now reports `rights_not_clear:*` and masks the other preconditions. Two acceptable designs:

- **R-A (recommended, minimal)**: keep rights first; `decision_reason` stays `rights_not_clear:<gate reason>`; additionally expose the full set of failed preconditions (`denial_reasons: [...]`) on the `PublicationJob`. Tests that assert another reason register a CLEAR rights record first, so each test observes its own precondition.
- **R-B**: evaluate all gates, report the highest-precedence failure (kill switch → operating mode → visibility → owner approval → rights) as `decision_reason`, with the rest in `denial_reasons`.

R-A is smaller and matches the acceptance criteria already written into `NEXT_BUILD_PLAN.md`. R-B is friendlier to operators. Either is defensible; what is *not* acceptable is leaving the reason masked with no list of the other failures.

### W1.1 — Shared CLEAR-rights fixture

| Field | Detail |
|---|---|
| **Scope** | `services/api/tests/conftest.py` (new) — a `clear_rights` fixture/helper |
| **Implementation** | Promote the working `_clear_rights(engine, item_id)` helper out of `tests/test_publication_rights_enforcement.py` into the shared `conftest.py`, returning the `record_id` so a test can revoke it. It must register creator → source → source item → rights record → status transition to `CLEAR` → one provenance step, because both `RightsLedgerStore.register_record` and `ProvenanceStore.append_step` require the source item to exist first. |
| **Acceptance criteria** | One import gives any test a CLEAR rights item in the right engine. |
| **Risk** | Low. The helper is proven — it is the code the 13 green enforcement tests already use. |

### W1.2 — Repair the 10 route-level publication tests

| File | Failures | Fix |
|---|---|---|
| `tests/test_public_publishing_gate.py` | 5 | Register a CLEAR rights record for the item each test publishes, **before** the publication. The two tests that assert `decision_reason == "owner_approval_required"` then observe the owner gate (their actual subject) instead of the rights gate. |
| `tests/test_tiktok_adapter.py` | 3 | 2 direct `submit_publication(...)` calls: pass `rights_clear=True` explicitly (the subject of those tests is adapter selection, not the rights gate). 1 route test: register CLEAR rights. |
| `tests/test_meta_adapter.py` | 2 | Same as the TikTok cases. |

**Acceptance criteria**: every test asserts the behaviour it names, and each route-level test that expects a publication exercises the *real* enforcement path (gate consulted, item cleared) rather than bypassing it.
**Risk**: Medium — this is where "weakening a test to make it pass" would hide. The rule to hold: a test that publishes must state its rights precondition explicitly, and a test that *denies* must assert the reason names rights.

### W1.3 — Repair the qualification harnesses (the audit's real lesson)

`scripts/private_mode_qualification.py` — three structural defects, all confirmed by reading the code:

1. **P4 is vacuous.** `invariant_rights_gate_refuses_uncleared` (`:342-380`) checks `body.get("status") == "PUBLISHED"`. The API returns `SUCCEEDED`, never `PUBLISHED`, so the invariant has never failed — it passed while the rights gate was completely unenforced. Fix: assert `status == "DENIED"` **and** that the reason names rights.
2. **P1/P2/P3 pass for the wrong reason.** They assert only `DENIED`. With rights evaluated first, a denial for rights reasons satisfies them. Fix: register CLEAR rights for each probe item, then assert the *specific* reason (`mode_..._denies_publishing`, `visibility_not_allowed`, `public_enabled` refusal).
3. **P5 now fails for the wrong reason.** The kill-switch probe publishes an uncleared item, so it is denied by rights before the kill switch is ever consulted; P5 asserts `"kill_switch" in reason` and fails. Fix: give the kill-switch probe a CLEAR rights item so the kill switch is what is actually tested.

**The pipeline walk** (`:718-735`) publishes `storage://qual-video` with no rights record, so the publication is `DENIED`, `external_id` is `None`, the analytics step then posts `external_id=None` and the walk aborts before the revenue stage — which is exactly the `stage not walked: revenue` failure. Fix: the harness must establish rights for `storage://qual-video` **before** the publish step, over HTTP, in this order:

```
POST /creators                              {creator_id, org_id, display_name, contact_ref}
POST /sources                               {source_id, org_id, creator_id, source_type, canonical_url}
POST /source-items                          {item_id, org_id, source_id, external_ref, captured_at, content_ref}
POST /rights-records                        {record_id, item_id, org_id, status: REVIEW_REQUIRED, basis, reviewed_by}
POST /rights-records/{record_id}/status     {org_id, to_status: CLEAR, reason_ref}
POST /source-items/{item_id}/provenance/steps  {org_id, step: {...}}
```

then `POST /publications` with `content_ref` = the item id (or `rights_item_id` = the item id). The harness must also assert the publication body is `SUCCEEDED`, not merely HTTP 200 — a `DENIED` job is HTTP 200, which is how a broken publish step can hide.

`scripts/security_qualification.py` C5 is already repaired in `4e0b049` and passes against the branch's code. It needs no further change, but it must be re-run against a live API started from the worktree (see W1.5).

### W1.4 — Repair the 3 monkeypatched `leaky` wrappers

`tests/test_private_mode_qualification.py:219 :242 :265` monkeypatch `publishing.evaluate_publish_policy` with wrappers of the pre-change signature. The route now calls with 5 positional arguments → `TypeError` → HTTP 500 inside the route. Fix: give each wrapper the new signature (`..., account_approved=False, rights_clear=False`) and forward it. These three tests exist to prove the harness *detects* a broken evaluator — they must keep proving that.

### W1.5 — Verification and landing

| Field | Detail |
|---|---|
| **Acceptance criteria** | 1. Full CI-env suite green: **0 failures** (baseline for branch code: 16 failures today, 1826+16 ≈ 1842 tests expected). 2. Coverage ≥ the 94.05 % recorded in the audit. 3. `ruff format --check`, `ruff check`, `mypy services/api/clipos_api` clean. 4. C5 demonstrated **red** against `main`'s code and **green** against the branch's code (both already observed: red = "publication of an item with no rights record was SUCCEEDED (reason=allowed), expected DENIED"; green = pass). 5. `scripts/private_mode_qualification.py` run against a live API started **from the worktree** reports `verdict: PASS`, `steps_failed: 0`, `invariants_failed: 0`, and every invariant's evidence names the precondition it tested. 6. `scripts/security_qualification.py` reports 11/11. 7. CI `baseline-ci.yml` green on the pushed SHA. |
| **Commands** | §7. |
| **Risk** | Medium — touches the publication hot path and the qualification harnesses. Mitigations: fail-closed default preserved; idempotency replay semantics unchanged; the 13 enforcement tests and 39 publishing tests already pin the semantics. |
| **Landing** | Commit on `fix/p0-1-rights-enforcement`; merge `--no-ff` into `main`; push; poll CI. Renew the expired `P0-1-RIGHTS` lease first, and release it plus remove the worktree afterwards. |

### W1.6 — Document the rights linkage contract (small, but required)

`PublishSubmission.rights_item_id` is optional and falls back to `content_ref`. The gate is keyed by the **rights registry item id**, and the provenance manifest is looked up as `prov-<item_id>`. So today a submission publishes only if a rights record exists whose `item_id` equals the `content_ref` string (or the explicit `rights_item_id`), *and* a provenance manifest exists for that item. That is a sharp, undocumented edge. Required:

- Document the contract in the API docs: register creator → source → source item → rights record → `CLEAR` → provenance step, then publish with `rights_item_id` set explicitly. Recommend making `rights_item_id` **required** for new submissions, with `content_ref` fallback retained for compatibility.
- Consider rejecting a submission whose `rights_item_id` is absent (fail closed with a precise reason) instead of silently keying on `content_ref`.
- The `denial_reasons` list from W1.0 makes the failure actionable for an operator who does not know this contract.

**Risk**: changing `rights_item_id` to required is a breaking API change → do it as a separate, announced commit after W1.5 lands.

---

## 5. W2 — Adapter provenance (P0-2, from `NEXT_BUILD_PLAN.md`)

**Why**: `status: SUCCEEDED` from a mock adapter is indistinguishable from a real upload. This is the most likely way an operator misreads the system's state.

| Field | Detail |
|---|---|
| **Priority** | P0 (after W1) |
| **Dependency** | W1 (same publication path); independent of W3 |
| **Scope** | `publishing.py`, `analytics.py`, `revenue_ledger.py`, `transcription.py`, `generation_adapters.py`, `llm_gateway.py`; response models; `apps/web` |
| **Implementation** | Give every adapter a declared `kind: "mock" | "real"` (a class attribute on the adapter, or a field on the health/result model) and surface it on every adapter-backed response: publication jobs, analytics collection results, revenue syncs, transcription results, generation results, provider dispatch results. The registry already reports adapter health — reuse that path rather than inventing a second mechanism. |
| **Acceptance criteria** | 1. Every response that was produced by an external-effect adapter states `adapter_kind`. 2. A mock adapter is labelled `mock` in the API response, in the evidence reports, and in the UI. 3. The E2E qualification asserts the field is present. |
| **Tests** | Field present on all adapter-backed responses; the mock adapter reports `mock`; a fake `real` adapter reports `real`. |
| **Risk** | Low — additive response fields. |

## 6. W3 — Cross-tenant isolation suite (P0-3)

**Why**: every table is org-scoped, and nothing in the test suite attempts to read or mutate org A's data as org B. For a multi-tenant control plane this is the highest-value untested invariant, and it is a security control, not a feature.

| Field | Detail |
|---|---|
| **Priority** | P0 |
| **Dependency** | none — can run in parallel with W1/W2 |
| **Scope** | `services/api/tests/test_cross_tenant_isolation.py` (new) |
| **Implementation** | A parametrised suite over every org-scoped route with a stored resource: create the resource in org A, then attempt every read/update/delete/collection as org B and assert 404/403 — never data, never a silent success. Derive the route list from the OpenAPI schema so the suite cannot silently skip a newly added route. Note the tenancy module's stores are in-memory (`tenancy.py` `OrgStore`/`MembershipStore`), while the domain stores are DB-backed; the suite must state which of the two it is exercising. |
| **Acceptance criteria** | Every org-scoped GET/POST in the OpenAPI contract is enumerated; each has an attempt to cross the boundary; failures are genuine boundary violations, not missing fixtures. A deliberate removal of an org filter must make the suite fail. |
| **Tests** | The suite itself, plus a mutation check proving the suite detects a removed filter. |
| **Risk** | Low to write; potentially revealing — which is the point. A failure here is a real finding, not a test bug, and must be reported as such. |

## 7. W4 — Wire the proven gates into CI (P1-1)

**Why**: `scripts/schema_drift.py` and `scripts/autonomy_evidence.py` are proven and enforced nowhere. The C5 hole existed because a control that was not run could not protect anything.

| Field | Detail |
|---|---|
| **Priority** | P1 |
| **Dependency** | W1 (the extended C5 must be green first) |
| **Scope** | `.github/workflows/baseline-ci.yml` |
| **Implementation** | Add `schema_drift.py`, `autonomy_evidence.py`, and the extended `security_qualification.py` C5 to the `validate`/`test` jobs. C5 needs a live app: the `build` job already has one for the E2E smoke — add the qualification run there, or start the API in the `test` job. |
| **Acceptance criteria** | CI fails if any of the three harnesses regresses. A deliberate one-line change to a harness's expectation must turn CI red. |
| **Tests** | The harnesses themselves (already present) + a CI dry run. |
| **Risk** | Low — but do it *after* W1, or CI will start red on a control that is mid-fix. |

## 8. W5 — First production month (P1)

Ordered, with the owner gate each one depends on:

| Item | Scope | Depends on |
|---|---|---|
| **W5.1 Scheduled durable jobs** (P1-2) | Bind the runbook's cadences to Temporal schedules: backup creation, restore test, analytics sync, cost reconciliation, credential-expiry monitor, SLO measurement. Acceptance: schedules visible in Temporal; `GET /backups/status` and `GET /slo/qualification` driven by real runs. | W4 |
| **W5.2 Identity/session/RBAC E2E** (P1-3) | Exercise sign-in → session → capability check → denial against the real stores; add the capability-matrix tests the model implies. Acceptance: a user without a capability is denied on every route that requires it. | — |
| **W5.3 Observability and alert delivery** (P1-4) | Complete trace/metric exporter wiring; connect the modelled alert conditions to a notification destination. Acceptance: a synthetic failure produces a delivered notification; one full request path shows traces. | W4 |
| **W5.4 Real adapters** (P1-5) | Real YouTube/TikTok/Meta, STT, TTS, generation behind the same registration contract the mocks use — no adapter-resolution change required. Acceptance: a recorded private test passes against the real platform, then owner approval per CLIP-099. | **owner credentials (human gate)** |
| **W5.5 Provider credentials** | Export provider API keys on the deployment host; re-run `scripts/test_llm_dispatch.py`; flip route health to `ok`. | **owner act** |

## 9. W6 — Hardening (P2)

- **Vetted AEAD for backups** — replace the stdlib `sha256-ctr + HMAC-SHA256` envelope with AES-GCM/ChaCha20-Poly1305; keep version-1 envelopes readable (the migration path is already documented).
- **Frontend breadth** — 5 pages against 261 API paths. Prioritise the operator loops that hurt: publication queue, rights status, incident timeline, cost/budget.
- **Frontend test framework** — the only domain with zero automated tests. Treat as a coverage gap, not a pass.
- **Connector failure simulation** — extend the mocks to emit 429/5xx/timeout so retry/backoff paths are genuinely exercised.
- **Dependency pinning and audit cadence** — pin tool versions in CI (an unpinned `ruff>=0.6` broke formatting mid-build) and schedule `pip-audit`/SBOM lanes.
- **`M-1` milestone bookkeeping** — add `M-1` to `completed_milestones` (proposed in the gap matrix, not applied). Do it in the same commit as the state work, never silently.

## 10. Decisions that need the owner

| # | Decision | Recommendation |
|---|---|---|
| 1 | Denial-reason semantics (W1.0) | R-A: rights first, plus a `denial_reasons` list |
| 2 | `rights_item_id` required on new submissions? | Yes, as a separate announced commit after W1.5 |
| 3 | Does P0-1 get a ticket? | **No new ticket** — it is a fix to frozen work. Keep the ledger at 108/108 so `release/v1.manifest.json` stays valid; record the fix in `docs/BACKLOG.md` (P0), `docs/reports/audit.md`, and `docs/reports/STATE_AUDIT.jsonl`. If the owner prefers a tracked ticket, add `CLIP-103` and re-freeze as v1.0.1 — `release_freeze.py` F3 compares completed ↔ ids ↔ declared total, so the manifest must be amended with it. |
| 4 | `submit_publication(..., rights_clear=True)` is a bypass surface for any in-process caller | Acceptable today (the route is the only production caller and the default is fail-closed). Consider accepting a `RightsGateDecision` object instead of a bool so a caller cannot assert clearance without a decision. |
| 5 | Where the plan document lives | Committed to the branch, so it lands with W1 |

## 11. Bookkeeping protocol

1. Renew the expired `P0-1-RIGHTS` lease (`scripts/agent_lease.py acquire` with `--steal-reason` if needed) **before** touching the worktree.
2. `BUILD_STATE.json` and `10_TICKETS.yaml` mutate only in the canonical checkout, only after gates pass, only with evidence — per `.agent/RUNNER_CONVENTIONS.md`. For W1 the correct action is **no ledger change** (see decision 3).
3. `docs/reports/STATE_AUDIT.jsonl` is append-only; a post-freeze fix line is appended with the real commit SHA.
4. `scripts/validate_handoff.py`, `scripts/validate_state.py`, `scripts/state_audit.py verify` must pass after any state mutation.
5. `scripts/release_freeze.py` must still pass after W1 lands (the manifest requires the six qualification harnesses to exist and the ledger to be 108/108 — W1 changes none of that, but `private_mode_qualification.py` is one of the six, so its behaviour change is inside the freeze's surface and must be re-verified explicitly).
6. Release the lease and remove the worktree after landing.

## 12. Canonical verification commands

```bash
# 0. bind the code under test to the checkout (see W0 — mandatory until the
#    conftest guard exists, and harmless afterwards)
export REPO=/srv/clipos/clipOS          # or /srv/clipos/worktrees/P0-1-RIGHTS
export PYTHONPATH=$REPO/services/api
cd $REPO

# 1. static gates
/srv/clipos/e2e-venv/bin/ruff format --check services/api
/srv/clipos/e2e-venv/bin/ruff check services/api
/srv/clipos/e2e-venv/bin/mypy services/api/clipos_api
bash scripts/secret_scan.sh
python3 scripts/architecture_guard.py
python3 scripts/deploy_topology_check.py --quiet
python3 scripts/validate_state.py
python3 scripts/validate_handoff.py
python3 scripts/state_audit.py verify
python3 scripts/release_freeze.py

# 2. full suite in the CI environment
sudo -u clipos -H env \
  DATABASE_URL=postgresql+psycopg://clipos_dev:clipos_dev_local_only@localhost:5433/clipos_dev \
  STORAGE_ENDPOINT=http://localhost:9002 STORAGE_ACCESS_KEY=clipos_dev \
  STORAGE_SECRET_KEY=clipos_dev_local_only STORAGE_BUCKET=clipos-test \
  CACHE_REDIS_URL=redis://localhost:6380/0 TEMPORAL_ADDRESS=localhost:7233 \
  /srv/clipos/e2e-venv/bin/pytest -q services/api

# 3. harnesses against a live API started FROM THE WORKTREE
#    (start from services/api so .agent/audit resolves correctly; chown -R
#    clipos:clipos <worktree>/services/api/.agent first — the rights gate
#    writes an audit event on every evaluation)
python3 scripts/security_qualification.py
python3 scripts/private_mode_qualification.py
```

## 13. Hazards (carried forward from `HANDOFF.md` §4, plus new)

- **Never run pytest in a worktree without pinning `PYTHONPATH`** — proven above; use the conftest guard (W0) to make it impossible to forget.
- Run pytest as `clipos` (`sudo -u clipos`); root creates root-owned `/tmp/*.log` files that break later runs.
- `chown -R clipos:clipos <worktree>/services/api/.agent` before running the API; the rights gate now writes an audit event on **every** evaluation, so an unwritable audit log will surface as `rights_gate_error:*` on every publication.
- Start the API from `services/api`, not the repo root, or `.agent/audit` resolves to the wrong directory.
- PowerShell 5.1: `&&` is not a separator; issue separate `ssh` calls. Do not use inline `python -c` through PowerShell — write a `.py` file, `scp` it, run it.
- `Add-Content -Encoding UTF8` inserts a BOM mid-file; use `[System.IO.File]::WriteAllText` with `UTF8Encoding($false)`.
- The `ci-results` recorder lags ~1 commit; a missing record usually means the next one is pending.
- `sed -i 's/\r$//'` every scp'd text file.

---

## 14. What the next session should do, in order

1. Renew the `P0-1-RIGHTS` lease; read this plan.
2. **W0** — add the `conftest.py` import-path guard; document the run command; re-run the suite to confirm the guard fires when unpinned and passes when pinned.
3. **W1.0** — get the denial-reason decision (R-A or R-B).
4. **W1.1 → W1.4** — shared fixture, 10 route-level tests, the three qualification harnesses, the 3 `leaky` wrappers.
5. **W1.5** — full suite green, C5 red/green proof, both harnesses green against a worktree-started API, CI green, merge `--no-ff`, release the lease, remove the worktree.
6. **W1.6** — document the rights linkage contract (and decide on making `rights_item_id` required).
7. **W2** and **W3** in parallel; then **W4**; then **W5** (owner-gated); then **W6**.
