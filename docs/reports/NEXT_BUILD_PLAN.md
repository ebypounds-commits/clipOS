# ClipOS — Next Build Plan

**Derived from**: `CURRENT_IMPLEMENTATION_AUDIT.md`, `PRD_GAP_MATRIX.md`, `FUNCTIONAL_TEST_REPORT.md` (commit `be4acd2`)
**Principle**: sequence by *actual* state and risk, not by the historical ticket order. The old order is complete; what remains is not "more tickets" but closing the gap between a well-built API and an enforced, operator-usable system.

---

## P0 — Must fix before any production use

### P0-1. Enforce the rights gate on the publication path

**Dependency**: none. **Scope**: `services/api/clipos_api/publishing.py`, `clipos_api/rights_gate.py`, one migration.
**Why first**: this is the PRD's hardest requirement and the only `BROKEN` classification. An unenforced rights gate means the system can publish content it cannot demonstrate rights for — a legal exposure, not a bug of convenience.

Work:
1. Establish the linkage: publications carry a `rights_item_ref` (or resolve an existing mapping from `content_ref`) that a rights record can be looked up by.
2. Call the gate inside `submit_publication` **before** the adapter runs, exactly as the owner gate (CLIP-099) is invoked today — same placement, same fail-closed default.
3. Denial must produce a `DENIED` job with reason `rights_not_clear`, audited, with the adapter never invoked.
4. Migration: add the linkage column/index (CREATE-only, follows the 0073 chain).
5. Extend the security qualification: C5 must attempt a **publication**, not just a gate evaluation, and assert `DENIED`. This is the harness gap that let the defect through.

**Acceptance criteria**
- An item with no rights record cannot be published at any visibility; response is a persisted `DENIED` job naming the rights reason.
- An item with `rights_status = CLEAR` publishes normally (no false positive).
- A revoked/expired rights record re-blocks publication (mirrors the owner-gate revocation test).
- `security_qualification.py` C5 fails if enforcement is removed.

**Tests**: publication-path denial; clear→allowed; revocation re-blocks; C5 regression (deliberately unenforced gate must FAIL the control); migration up/down.
**Risk**: medium — touches the publication hot path. Mitigation: fail-closed default plus the existing idempotency-key replay semantics, and the CLIP-099 placement is a proven template.

---

### P0-2. Make adapter provenance visible in API responses

**Dependency**: none. **Scope**: publishing, analytics, revenue, transcription, generation adapters; response models.
**Why**: the mocks are honest in their docstrings but indistinguishable in output. `status: SUCCEEDED` from a Mock adapter reads as a real upload — the single most likely way an operator misreads this system's state.

Work: add `adapter_kind: "mock" | "real"` (or equivalent) to publication results, analytics collection results, and generation results; surface it in the UI and in evidence reports.

**Acceptance**: every external-effect response states whether a real or mock adapter served it; the E2E qualification asserts the field is present.
**Tests**: field present on all adapter-backed responses; real adapter (once wired) reports `real`.
**Risk**: low.

---

### P0-3. Cross-tenant isolation test suite

**Dependency**: none. **Scope**: `services/api/tests/`.
**Why**: tenant fields exist everywhere, but nothing attempts to read or mutate org A's data as org B. For a multi-tenant control plane this is the highest-value untested invariant.

Work: a parametrised suite that, for every org-scoped route with a stored resource, creates the resource in org A and asserts org B receives 404/403 — never data, never a silent success.

**Acceptance**: the suite enumerates every org-scoped GET/POST and covers each; failures are genuine boundary violations.
**Tests**: the suite itself; a deliberate boundary-removal must fail it.
**Risk**: low to write, potentially revealing (which is the point).

---

## P1 — First production month

### P1-1. Wire the enforced gates into CI
Add `schema_drift.py`, `autonomy_evidence.py`, and (once P0-1 lands) the extended security qualification to the CI `test`/`validate` jobs. All are proven; none is currently enforced. Closes the "gate exists but does not run" class of gap that allowed the rights defect to ship.
**Acceptance**: CI fails if any harness regresses. **Risk**: low.

### P1-2. Scheduled durable jobs
Bind the runbook's daily/weekly/monthly cadences to Temporal schedules rather than manual invocation: backup creation, restore test, analytics sync, cost reconciliation, credential-expiry monitor, SLO measurement.
**Dependency**: P1-1 for observability. **Acceptance**: schedules visible in Temporal; `GET /backups/status` and `GET /slo/qualification` driven by real runs. **Risk**: medium (scheduling semantics).

### P1-3. Identity, session, and RBAC end-to-end
Exercise the full path: sign-in → session → capability check → denial, against real stores. Add the capability matrix tests the authorization model implies.
**Acceptance**: a user without a capability receives a denial on every route that requires it. **Risk**: medium.

### P1-4. Observability and alert delivery
Complete the trace/metric exporter wiring and connect the modelled alert conditions to a notification destination. An incident system nobody is told about is only half-built.
**Acceptance**: a synthetic failure produces a delivered notification; traces visible for one full request path. **Risk**: low-medium.

### P1-5. Real adapters as credentials arrive
Real YouTube, TikTok, Meta, STT, TTS, and generation adapters behind the same registration contract the mocks use — no adapter-resolution changes required. Each lands with its platform's quota/retry semantics and a recorded private test.
**Dependency**: owner credentials (human gates). **Acceptance**: private test passes against the real platform, then owner approval per CLIP-099. **Risk**: high (external), mitigated by the mock contract already proven end-to-end.

---

## P2 — Hardening

- **Vetted AEAD for backups** — replace the stdlib `sha256-ctr + HMAC-SHA256` envelope with AES-GCM/ChaCha20-Poly1305; keep version-1 readable (migration path already documented).
- **Frontend breadth** — the API is ~50× the UI surface. Prioritise the operator loops that hurt most: publication queue, rights status, incident timeline, cost/budget.
- **Frontend test framework** — the only domain with zero automated tests.
- **Connector failure simulation** — extend mocks to emit 429/5xx/timeout so retry/backoff paths are genuinely exercised.
- **Dependency pinning and audit cadence** — pin tool versions in CI (an unpinned ruff broke formatting mid-build) and schedule `pip-audit`/SBOM lanes.
- **M-1 milestone bookkeeping** — add `M-1` to `completed_milestones` (proposed in the gap matrix; not applied).

---

## Explicit non-goals for the next phase

- No new product domains. 26 domains are built or partially built; the constraint is depth and enforcement, not breadth.
- No ticket-count theatre. The 108-ticket ledger is complete and consistent; the remaining work is not well expressed as more tickets of the same shape.
- No production deployment, no public posting, no external account mutation — those remain owner human gates.

---

## Recommended sequence

```
P0-1 rights enforcement ──┬─→ P1-1 wire gates into CI ──→ P1-2 scheduled jobs
                          └─→ P0-2 adapter provenance
P0-3 cross-tenant suite (parallel, independent)

then: P1-3 identity E2E · P1-4 observability · P1-5 real adapters (credential-gated)
then: P2 hardening
```

P0-1 is the gate on everything else: until the rights claim is true end-to-end, no amount of additional capability makes the system safe to operate.
