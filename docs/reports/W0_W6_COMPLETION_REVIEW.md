# ClipOS Continuation Review — W0–W6 Completion, Errors, and Recommendations

**Date**: 2026-11-13
**Repository**: `/srv/clipos/clipOS` (VPS, authoritative)
**Baseline**: `ed3bdb8` (W0–W4 merged through the previous sessions)
**Reviewed tip**: `2608e80` (five commits pushed this session: `4d5be6d`, `af52185`, `52613c0`, `37fe407`, `2608e80`)

This report is the deliverable of the continuation goal: it records what
W0–W6 now contain, every error found and fixed on the way, and the
issues that remain — each with a recommendation, for owner review.

---

## 1. Status of the work packages

| Package | Status | Evidence |
|---|---|---|
| **W0** test-evidence integrity | **DONE** | `services/api/tests/conftest.py` aborts any run whose `clipos_api` import resolves to another checkout; procedure documented in `.agent/RUNNER_CONVENTIONS.md` and `docs/development.md`; guard unit-tested (3 tests). |
| **W1** P0-1 rights enforcement | **DONE** (merged earlier as `d3a07ea`) | Rights gate enforced on every publication path, fail-closed; `denial_reasons` persisted (migration 0074); qualification harnesses repaired so their controls can actually fail; rights linkage contract documented in `07_SECURITY_GOVERNANCE_RIGHTS.md`. |
| **W2** adapter provenance (P0-2) | **DONE this session** (`52613c0`) | Every mock result now reports `mock`; jobs and policies derive the kind from the platform's registered adapter at read time (no column, no drift); 26 acceptance tests in `test_adapter_provenance.py` + 4 augmented mock-path tests. Two read paths remain truthfully-unanswerable — see R1. |
| **W3** cross-tenant isolation (P0-3) | **DONE** (merged as `ed3bdb8`) | 9 explicit resource families created as org A and probed as org B, plus mutation and duplicate-identifier tests; org-scoped lookups and per-org unique indexes (migrations 0076/0077). |
| **W4** CI gates | **DONE and now proven** | `schema_drift.py`, `autonomy_evidence.py`, and both qualification harnesses run in CI (`efd42e4`). This session ran `scripts/qualification_ci.sh` end-to-end locally: image build → live container → security 11/11 → private-mode 34 steps / 6 invariants → `qualification_ci: ok` — the first real verification of that CI path. |
| **W5** first production month | **PARTIAL** | Done (earlier session): `/metrics` endpoint, collection-scheduler CLI, cron script. Not done: identity/session live E2E, alert delivery, Temporal cadence schedules, real adapters — R2–R5 below. |
| **W6** hardening | **PARTIAL** | Done: dependency pinning (earlier session), **M-1 bookkeeping this session** (`37fe407`). Not done: AEAD backup envelope, frontend breadth, frontend test framework, connector fault injection — R6–R8, R11 below. |

---

## 2. Errors found and fixed (this session)

| # | Error | Root cause | Fix |
|---|---|---|---|
| E1 | CI `baseline-ci` **red on `ed3bdb8`** at the `ruff_format` step (test job never ran) | Two test files merged unformatted; one blind `except Exception` | `4d5be6d`: formatted both files, narrowed the catch to `sa.exc.SQLAlchemyError`. Verified `ruff format --check` (320 files) and `ruff check` clean. |
| E2 | `test_offline_sql_renders_create_only_schema` **failed without `DATABASE_URL`** | Migration 0076 called `op.drop_constraint`; SQLite's operations impl raises `NotImplementedError` before any SQL renders. CI never saw it because CI sets `DATABASE_URL` (Postgres dialect) | `af52185`: dialect-aware drop (Postgres → `DROP CONSTRAINT`, else `DROP INDEX IF EXISTS`), mirroring 0077's pattern. Verified passing with `DATABASE_URL` unset. |
| E3 | **W2 was declared but not wired** — only analytics health set `adapter_kind`; every mock publication/transcription/sync reported `real`; the field had landed on `PublishPolicy`, not `PublicationJob` | The earlier W2 commit added response fields but populated almost nothing | `52613c0`: full wiring (see §1) + 26-case acceptance suite. |
| E4 | Nine class declarations sat **before** their docstrings, silently discarding `__doc__` | Attribute inserted immediately after `class X:` | Same commit: declarations normalized to after the docstring. |
| E5 | Two `test_autonomy_evidence` tests red | They assert the CI record is healthy for the current HEAD; HEAD's recorded run was a red one (tripwire working as designed) | **Closed**: record for `2608e80` is `status=success`; the tests were re-run against it and pass 23/23. |
| E6 | (earlier) Worktree test runs silently executed **`main`'s code** | Shared venv editable install maps `clipos_api` to the canonical checkout | W0 conftest guard — see §1. |
| E7 | (earlier) Every audited API action would **500 in the container image** | `/app` owned by root, API runs as `clipos`; audit log path not writable | Dockerfile creates `/app/.agent/audit`; `AUDIT_DIR` env override; tests pin directory creation. |
| E8 | (earlier) **Seven tables** could not accept inserts on Postgres; rights registry ids collided across orgs | Raw-DDL migrations created `id INTEGER NOT NULL` with no sequence; global unique indexes on org-scoped data | Migrations 0075 (sequences), 0076 (per-org indexes), 0077 (creator/source/fingerprint registries). |
| E9 | (earlier) Qualification controls that **could not fail** or passed for the wrong reason | P4 compared against the never-returned `PUBLISHED`; P1–P3 asserted only `DENIED` | Harnesses now clear rights first and assert their own reason; security harness re-runnable (per-run platform ids). |
| E10 | My own first W2 attempt **stripped base classes** (`class OpenAICompatAdapter(_BaseAdapter)` → `class OpenAICompatAdapter`) via a greedy regex, breaking 7 LLM tests | Regex `class X[^:]*:` consumed the inheritance clause | Attempt discarded (stash `stash@{0}` kept for reference), re-implemented with anchored patterns; superseded by `52613c0`. |
| E11 | CI `test` job **red at the migrations step** for `37fe407`: `UndefinedObject: constraint "uq_source_items_item_id" does not exist` | Migration 0076 assumed the dialect decides the object form (Postgres → `DROP CONSTRAINT`), but only `provenance_manifests` carries a constraint; `source_items` has a plain unique index. The dev database had been upgraded by the earlier index-based version, so only CI's from-scratch run could see it | `2608e80`: inspect unique constraints vs indexes and drop what exists (0077's proven pattern). Verified two ways before pushing: **fresh-database `alembic upgrade head` (0001→0077) succeeds** and `schema_drift` against that fresh schema reports 0 divergences; offline render still passes without `DATABASE_URL`. |

---

## 3. Verification evidence (this session)

| Gate | Result |
|---|---|
| `ruff format --check services/api` | 320 files clean |
| `ruff check services/api` | all clean |
| `mypy services/api/clipos_api` | 0 errors (112 files) |
| Full CI-env suite (final, after all fixes) | **1996 passed, 0 failed**, 880 warnings, 94.7 % coverage |
| W2 targeted suites | 189 passed |
| Offline migrations (no `DATABASE_URL`) | pass |
| `validate_state` / `validate_handoff` / `state_audit verify` / `release_freeze` | all pass after M-1 |
| `scripts/qualification_ci.sh` local preflight | **PASS** — security 11/11, private-mode 34 steps / 6 invariants, `qualification_ci: ok` |
| `test_autonomy_evidence` after green CI | **23 passed** (tripwire closed) |
| CI for `2608e80` | **status=success** — validate, test, quality, build all green; includes qualification + autonomy + schema_drift gates (§4) |
| Fresh-database `alembic upgrade head` (0001→0077) | pass; `schema_drift` on that schema: 0 missing tables, 0 breaking divergences, 0 missing defaults |

---

## 4. CI result for the pushed tip

**Green.** Run `43429042388` for `2608e8018b1933aaf2a212ddfd21ea9f00e62f55`:

```
status=success
jobs=validate:success, test:success, quality:success, build:success
```

This is the first fully green CI run that includes the W4 gates — the
qualification harnesses ran inside CI's test job against a live API
container (`qualification_output.txt`), the autonomy evidence report ran
in the validate job, and `schema_drift` ran against the migrated
database. The two `test_autonomy_evidence` tripwire tests were re-run
locally against this record: **23 passed** — the record-dependent
failures (E5) are closed.

The run also proves the two fixes that only a from-scratch environment
can exercise: the offline SQLite render of migration 0076 (E2) and the
fresh-Postgres drop of the right object (E11) both executed on CI's
clean database.

---

## 5. Remaining issues and recommendations (for review)

### R1 — W2 residual: two read paths cannot yet answer truthfully (Medium)
`GET` of an analytics snapshot rebuilds the response from stored metric
rows, and image/video generation responses rebuild from media rows;
neither has an adapter in scope, so they still carry the field default
(`real`). **Recommendation**: snapshots carry `platform` already —
introduce a platform-keyed analytics adapter registry (mirroring
publishing's `get_adapter`) and derive at read; for generation, add an
`adapter_kind` column to `image_assets` and the video generation source
(a create-only deviation in the style of migrations 0075/0077), or add a
generation-provenance row. Prefer the registry for snapshots (no schema),
the column for generation (history must stay truthful when real adapters
land).

### R2 — Alert delivery has no destination (W5, needs owner decision)
Alert conditions exist but nothing delivers them. **Recommendation**:
`ALERT_WEBHOOK_URL` (or SMTP settings) as a secret; wire the existing
alert conditions to POST there; add one CI/harness probe that fires a
synthetic alert. Owner supplies the destination — deferred until chosen.

### R3 — Temporal cadence schedules not installed (W5)
Backup, restore-test, analytics-sync and monitor cadences from the
runbook are not installed as Temporal schedules. **Recommendation**: a
`scripts/install_temporal_schedules.py` (idempotent) plus a runbook step;
deliberately not executed here — it mutates shared infra on the host and
should be reviewed before install.

### R4 — Real platform/provider adapters (W5, blocked on owner)
All four publishing adapters and all provider adapters are stand-ins or
need credentials. Blocked on the five human gates (`production_secrets`,
three platform accounts, `first_public_post`). **Recommendation**: when
credentials arrive, implement behind the existing `PublishingAdapter`
protocol and flip `adapter_kind` to `real` — the routing, tests and
reporting contract require no change (that is what W2 bought).

### R5 — Identity/session live E2E not written (W5)
**Recommendation**: `scripts/identity_qualification.py` in the
qualification-harness style (sign-in → session → capability check →
denial against live stores), then wire it into `qualification_ci.sh`
like the other harnesses.

### R6 — Backup envelope is not AEAD (W6)
Backups still use stdlib `sha256-ctr + HMAC-SHA256`. **Recommendation**:
AES-256-GCM (or ChaCha20-Poly1305) envelope, keep version-1 envelopes
readable for migration, add a known-answer test vector.

### R7 — Frontend breadth and zero frontend tests (W6)
Five pages against 261 API paths; no frontend test framework at all.
**Recommendation**: introduce Vitest + Playwright first against the two
highest-value flows (sign-in, publication queue), then grow page
coverage by operator pain, not route count.

### R8 — Mock adapters cannot emit faults (W6)
Retry/backoff paths are never exercised. **Recommendation**: a single
`CLIPOS_MOCK_FAULT=timeout|429|5xx` env read by the mock adapters, plus
tests proving the retry wrapper handles each.

### R9 — `ruff>=0.6` is unpinned (W6)
The tree is formatted with ruff 0.17.1 but the constraint allows any
newer release; format drift between local and CI is exactly how `ed3bdb8`
went red. **Recommendation**: pin `ruff==0.17.1` in `services/api`
dev extras (the pinning pass covered other packages, not the linter).

### R10 — Repository hygiene leftovers (Low)
- `stash@{0}`: the discarded broken W2 attempt; `stash@{1}`: a stale
  `tsconfig.tsbuildinfo`. **Recommendation**: drop both after review.
- One skipped test (`test_dependency_security`) skips when `git` is
  absent from the `sudo -u clipos` PATH — environment noise, harmless.
- `clipos-dev-api-1` container idles on the dev host (compose `--profile
  app` leftovers); harmless but worth an occasional `compose down`.

---

## 6. State and protocol notes

- `BUILD_STATE.json`: `completed_milestones` now includes **M-1**
  (tickets `CLIP-P001..P006` were already complete; the release evidence
  text already claimed M-1 complete). Ticket ledger unchanged at 108/108;
  `current_milestone=NONE`, `status=M24_COMPLETE`. All five validators
  pass after the edit.
- Migrations applied to the dev database: **0074–0077**.
- No ticket was opened or closed; no human gate was touched; nothing was
  published anywhere.
