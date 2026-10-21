# ClipOS — Session Handoff

**Date**: 2026-10-20/21
**Repo**: `/srv/clipos/clipOS` (authoritative VPS checkout; local mirror at `C:\Users\Gozmar Dynamics\XiaomiMiMoProjects\Clipos`)
**Over SSH**: `ssh vps`

---

## 1. Project state

Autonomous build **complete**: 108/108 tickets, milestones M-1..M24 closed, `v1.0.0` frozen.
- `main` @ `be4acd2` (audit reports) / `13008ee` (artifact cleanup) — CI green
- `BUILD_STATE.json`: `current_ticket=NONE`, `status=M24_COMPLETE`, 108 completed, 25 milestones
- 112 API modules (~45k lines), 72 migrations (→0073), 261 OpenAPI paths, 121 test files
- 1,838 tests pass in CI env @ 94.05% coverage; 13/13 quality gates green
- Five human gates remain `false` (owner acts): production_secrets, youtube/tiktok/meta real accounts, first_public_post

## 2. What the audit found (4 reports in `docs/reports/`)

Read these first: `CURRENT_IMPLEMENTATION_AUDIT.md`, `PRD_GAP_MATRIX.md`, `FUNCTIONAL_TEST_REPORT.md`, `NEXT_BUILD_PLAN.md`.

**The one critical finding**: the rights gate is implemented, correct, and tested as a component, but `submit_publication` never consulted it. An item with no rights record published `SUCCEEDED` while `/rights-gate/<same item>/evaluate` returned 404. That is a hard PRD requirement (`rights_status != CLEAR → publication blocked`) — classified **BROKEN**.

**Why CI missed it**: security control C5 probed the gate endpoint in isolation and never attempted a publication. Both the gate and the control were individually correct; neither observed the enforcement path.

## 3. Work in flight: P0-1 (rights enforcement) — NOT LANDED

Worktree: `/srv/clipos/worktrees/P0-1-RIGHTS` (branch `fix/p0-1-rights-enforcement`), lease `P0-1-RIGHTS` owned by `mimo-clipos-p01`.

### Done and verified

1. **`services/api/clipos_api/publishing.py`** — enforcement implemented:
   - `PublishSubmission.rights_item_id: str | None` (falls back to `content_ref`)
   - `evaluate_publish_policy(..., rights_clear: bool = False)` — **first** check is `if not rights_clear: return False, "rights_not_clear"` (fail-closed default: a caller that ignores the gate cannot publish)
   - `_rights_cleared(request, engine) -> tuple[bool, str]` helper composing `RightsPublicationGate` over `RightsLedgerStore` + `ProvenanceStore`; **never raises** — returns `(False, "rights_gate_error:<Type>")` on any lookup problem
   - `submit_publication(..., rights_clear=False, rights_reason="")`; denial reason prefixed `rights_not_clear:<reason>`
   - `submit_publication_route` calls `_rights_cleared` and threads the results through
   - ruff + mypy clean (112 files)

2. **C5 harness fix** — `scripts/security_qualification.py` `check_rights_gate_denies` now attempts a **real publication** (not just the gate endpoint) and asserts `DENIED` with a rights reason, plus the original gate probe as a second assertion. Verified red against old code (`publication ... SUCCEEDED, expected DENIED`), and **green after the fix went live** (`VERDICT: PASS 11/11`).

3. **Tests** — `services/api/tests/test_publication_rights_enforcement.py`, **13 passing**. Covers: fail-closed default, denial names the rights reason, allowed when CLEAR, explicit `rights_item_id`, revocation re-blocks, denied job persisted+audited, idempotent replay, gate-error fail-closed.

4. **`services/api/tests/test_publishing.py`** — updated for the new signature. `rights_clear=True` added to 9 direct-call tests; route roundtrip gets a real CLEAR rights record. Also strengthened 3 tests that were passing *for the wrong reason* (asserting only `allowed is False`), now bound to their actual rule strings (`private_test_forbids_public`, `visibility_not_allowed`). **39/39 pass** across both files.

### Remaining work (the blocker)

Full CI-env suite: **16 failures** — all the same class: other test files exercise the publication route without cleared rights.

```
tests/test_private_mode_qualification.py    3 failures
tests/test_public_publishing_gate.py        5 failures
tests/test_tiktok_adapter.py                3 failures
(+ ~5 more; run the full suite to enumerate)
```

Two distinct causes:
- **(a)** Route-level tests need a CLEAR rights record for the item they publish (reuse `_clear_rights(engine, item_id)` from `test_publication_rights_enforcement.py`; the route keys the gate on `content_ref`).
- **(b)** One failure shows `rights_not_clear:rights_gate_error:OperationalError` — the gate raises under the test's engine. **Investigate this first**; it may indicate a real defect in `_rights_cleared` (e.g. the engine handed in by the route dependency differs from the one the test patched, or a table missing on the SQLite test engine). The gate logic itself is verified correct in isolation (`diag2.py` → `not close = not_found`, no error).

## 4. Tooling hazards discovered (important — cost most of the session)

**The write-truncation mystery is SOLVED**: it was never the write tool. Files failed to appear because **my tool-call emission degenerated into a text-repetition loop and the turn ended before the call executed**. Evidence: 10 KB and 15 KB files persisted fine; the missing ones were all in turns that degenerated. Turns where I narrated "let me write X" at length were the ones that failed.

**Reliable pattern — always do this:**
1. Keep each write small (< ~3 KB of content)
2. **Immediately `Test-Path` after every write** before using the file — non-negotiable
3. For larger files: write a small skeleton, then `Add-Content` to append chunks, verifying as you go
4. Never narrate before a tool call. Emit it directly.
5. PowerShell 5.1: `&&` is NOT a valid separator — issue separate `ssh` calls
6. Never use inline `python -c` with quotes through PowerShell — write a `.py` file, scp it, run it
7. `Add-Content -Encoding UTF8` introduces a BOM mid-file; strip with `[System.IO.File]::WriteAllText` using `UTF8Encoding($false)` when appending to existing source files

**Other recurring traps:**
- Run pytest as `clipos` (`sudo -u clipos`); running as root creates root-owned `/tmp/*.log` files that later break the clipos run
- `chown -R clipos:clipos <worktree>/services/api/.agent` before running the API — the audit log must be writable, and the rights gate now writes an audit event on every evaluation
- Start the API from `services/api` (not the repo root) or `.agent/audit` resolves to the wrong directory
- The `ci-results` recorder lags ~1 commit; a missing record usually just means the next one is pending
- Always `sed -i 's/\r$//'` on every scp'd text file

## 5. Immediate next steps

1. Diagnose the `rights_gate_error:OperationalError` (see §3b) — determine whether it is a real `_rights_cleared` defect or a test-engine artifact. Fix accordingly.
2. Add `_clear_rights(engine, item_id)` setup to the route-level tests in `test_private_mode_qualification.py`, `test_public_publishing_gate.py`, `test_tiktok_adapter.py`, and any others the suite names.
3. Run the full CI-env suite to zero failures.
4. Consider whether a migration is needed for `rights_item_id` (it is a request field only; no column added — confirm no persistence change is required).
5. Commit, merge `--no-ff` into `main`, push, poll CI.
6. **State transition**: this is a *fix* to already-complete work, not a new ticket. Decide whether to append to `docs/reports/audit.md` and whether BUILD_STATE needs any change (it likely does not — the 108/108 ledger stands). Document the decision either way.
7. Release the `P0-1-RIGHTS` lease and remove the worktree.

## 6. Then, from `NEXT_BUILD_PLAN.md`

- **P0-2** adapter provenance (`adapter_kind: mock|real` in responses) — the mocks are indistinguishable from real uploads in API output
- **P0-3** cross-tenant isolation suite — no test currently attempts org A's data as org B
- **P1-1** wire `schema_drift.py` and `autonomy_evidence.py` into CI (proven, unenforced)
- **P1-2** Temporal schedules for the runbook's cadences (backup, restore test, analytics sync, monitors)
- P1-3 identity/session E2E · P1-4 observability + alert delivery · P1-5 real adapters when credentials arrive
- P2: vetted AEAD for backups, frontend breadth, frontend test framework, dependency pinning

## 7. Key file map

```
services/api/clipos_api/publishing.py     ← P0-1 enforcement (in flight)
services/api/clipos_api/rights_gate.py    ← the gate (unchanged, correct)
services/api/tests/test_publication_rights_enforcement.py  ← 13 new tests
services/api/tests/test_publishing.py     ← updated for rights_clear
scripts/security_qualification.py         ← C5 now tests enforcement
docs/reports/*.md                         ← the four audit reports
docs/BACKLOG.md                           ← post-launch backlog
release/v1.manifest.json                  ← frozen release definition
```
