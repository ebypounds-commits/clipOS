# ClipOS — Functional Test Report

**Date**: 2026-10-20 · **Commit**: `be4acd2` · **Branch**: `main`
**Runtime**: VPS checkout `/srv/clipos/clipOS`; Python 3.14.4; Node 22.23.1; Docker 29.6.2; Compose v5.3.1; FFmpeg/FFprobe 8.0.1; PostgreSQL 18.6; git 2.53.0

---

## 1. Environment qualification (§9)

| Dependency | Status | Version |
|---|---|---|
| python3 | AVAILABLE | 3.14.4 |
| node | AVAILABLE | v22.23.1 |
| npm | AVAILABLE | 10.9.8 |
| docker | AVAILABLE | 29.6.2 |
| docker compose | AVAILABLE | v5.3.1 |
| ffmpeg | AVAILABLE | 8.0.1 |
| ffprobe | AVAILABLE | 8.0.1 |
| git | AVAILABLE | 2.53.0 |
| pg_dump | AVAILABLE | 18.6 |

No dependency was MISSING or BROKEN. Python environment used: `/srv/clipos/e2e-venv` (the repository's project environment on this host; the instruction's Windows `.venv\Scripts\python.exe` does not exist on this machine).

Infrastructure stack (compose): postgres, redis, minio, temporal, temporal-ui — all `healthy`.

---

## 2. Static quality gates (§11)

| Gate | Command | Exit | Result |
|---|---|---|---|
| ruff format | `ruff format --check services/api` | 0 | 306 files formatted |
| ruff check | `ruff check services/api` | 0 | All checks passed |
| mypy | `mypy services/api/clipos_api` | 0 | No issues in 112 files |
| secret scan | `bash scripts/secret_scan.sh` | 0 | clean |
| architecture guard | `python3 scripts/architecture_guard.py` | 0 | 6/6 OK |
| deploy topology | `python3 scripts/deploy_topology_check.py` | 0 | VERDICT PASS |
| release freeze | `python3 scripts/release_freeze.py` | 0 | F1–F7 PASS |
| validate state | `python3 scripts/validate_state.py` | 0 | valid (108 completed) |
| validate handoff | `python3 scripts/validate_handoff.py` | 0 | 108 tickets; current=NONE |
| state audit | `python3 scripts/state_audit.py verify` | 0 | 108/108 covered |
| web typecheck | `tsc --noEmit` (apps/web) | 0 | clean |

**11 of 11 static gates pass.**

---

## 3. Test suites (§12, §13)

### Backend — CI environment (PostgreSQL + Redis + MinIO + Temporal)

```
1838 passed, 0 failed, 0 skipped    coverage 94.05%   252s
```

### Backend — SQLite mode

```
1827 passed, 11 skipped, 0 failed   222s
```

**Two initial failures in a first SQLite run were harness artifacts, not product defects — investigated and dismissed:**

| Failure | Cause | Verdict |
|---|---|---|
| `test_migrations.py::test_offline_sql_renders_create_only_schema` | I passed `DATABASE_URL=""` (empty string). Alembic's offline render requires the variable *unset*; an empty string is unparseable. Re-run with `-u DATABASE_URL` → **passes**. | Harness artifact |
| `test_cache_integration.py::test_redis_rate_limiter_enforces_fixed_window` | Known flake under full-suite load; passes in isolation and in the CI-env run. | Flake (pre-existing, documented in project notes) |

### Frontend

| Check | Result |
|---|---|
| `next build` | success — routes `/`, `/agents`, `/models`, `/reviews`, `/reviews/[org]` |
| `tsc --noEmit` | clean |

No frontend unit/E2E test framework is configured in the repository; classified `NOT_APPLICABLE` for the current scope, with the caveat that this is a coverage gap rather than a pass.

---

## 4. Application startup (§16)

| Component | Status | URL/Port |
|---|---|---|
| API (uvicorn) | running | `127.0.0.1:8111` — `/health` → `{"status":"ok","version":"1.0.0"}` |
| Web (Next.js) | running | `127.0.0.1:3000` |
| PostgreSQL | healthy | `localhost:5433` |
| Redis | healthy | `localhost:6380` |
| MinIO | healthy | `localhost:9002` |
| Temporal | healthy | `localhost:7233` (UI 8233) |
| Worker | not started separately | worker module present; workflow execution via Temporal |

All services bound to localhost only; nothing exposed publicly.

---

## 5. API surface checks (§18)

### Read path — all 179 GET endpoints

```
endpoints probed:     179
  2xx/3xx:            106
  4xx (expected):      73   (missing query params / unknown ids — correct validation)
  5xx:                  0
  timeouts/hangs:       0
  slower than 5s:       0
slowest: 0.18s
```

**Zero unhandled server errors, zero incomplete terminations, no pathological latency.**

### Write path — all 169 POST endpoints

Minimal `{}` bodies sent deliberately to reach unguarded code paths:

```
endpoints probed:     169
  unhandled 5xx:        0
  hangs:                0
```

4xx validation responses are the correct outcome for empty bodies and are not counted as failures.

### OpenAPI

- 261 paths, 348 operations, 488,834 bytes of schema.
- 75 modules expose routers; 34 modules are domain/service layers without routers (expected).
- No placeholder-returning routes detected; no undocumented routes surfaced beyond the registry.

---

## 6. Functional walkthrough (§17, §19)

### End-to-end pipeline — VERDICT PASS

```
research    4 steps   planning     5 steps   produce   2 steps   render    2 steps
publish     3 steps   analytics    3 steps   revenue   4 steps   experiments 5 steps

28 steps, 0 failed, 0 skipped
6 invariants, 0 failed
38 responses scanned, 0 secret-shaped material
```

Invariants verified: OBSERVE denies all publishing · PRIVATE_TEST cannot publish PUBLIC · PUBLIC refused while `public_enabled=false` · rights gate refuses uncleared content (as a *component*) · kill switch denies · no secret leakage.

### Per-domain functional results

| Domain | Result | Note |
|---|---|---|
| Health/readiness | WORKING | `/health`, `/operator/health`, `/models/health` |
| Agent Registry | WORKING | Registered, versioned, evaluated, walked DRAFT→ACTIVE live |
| Model Registry | WORKING | 6/6 aliases resolvable + dispatchable; gaps name exact env vars |
| Rights gate (component) | WORKING | Correct decisions, tested |
| Rights gate (enforcement) | **BROKEN** | Not consulted by publication — see §7 |
| Publishing governance | WORKING | Owner gate, kill switch, modes all enforced |
| Media/render | WORKING | Real ffmpeg, `shell=False` argv, path-confined |
| Clip/creative/analytics/revenue/experiments | WORKING | All stages traverse |
| Platform adapters | MOCKED | By design; real integration awaits OAuth |
| STT/TTS/generation adapters | MOCKED | By design; await credentials |

### UI walkthrough

| Page | Status | Classification |
|---|---|---|
| `/` | 200, renders | FUNCTIONAL_UI |
| `/agents` | 200, live agent data | FUNCTIONAL_UI |
| `/models` | 200, live provider routes | FUNCTIONAL_UI |
| `/reviews` | 200, query-param org | FUNCTIONAL_UI |
| `/reviews/[org]` | 200, **fixed during this validation** (was 404) | FUNCTIONAL_UI |

Link sweep: 12 local links checked, **0 broken**; 0 external links. No `STATIC_MOCK_UI` surfaces.

---

## 7. Critical finding with reproduction

**Rights gate not enforced on publication.**

```bash
# policy allows the visibility; owner gate satisfied so CLIP-099 is not the blocker
POST /publish-policies      {allowed_visibilities:[PRIVATE,UNLISTED], public_enabled:true}
POST /publishing/private-tests   {outcome: PASSED}
POST /publishing/owner-approvals {granted}

POST /publications  content_ref="storage://NEVER-CLEARED-CONTENT"  visibility=PRIVATE
  -> 200  {"status":"SUCCEEDED","decision_reason":"allowed"}

POST /rights-gate/NEVER-CLEARED-CONTENT/evaluate
  -> 404  {"detail":"rights record not found"}
```

The item the gate declares not publishable is published. Code confirmation: `submit_publication()` contains **zero** references to rights/provenance/authorization/license; `content_ref` is an opaque string with no foreign key to any rights record.

**Why C5 in `security_qualification.py` does not catch this**: it calls `/rights-gate/{item}/evaluate` directly and asserts the 404. Both the gate and the control are correct in isolation; neither observes the publication path.

---

## 8. Blockers

| Category | Item |
|---|---|
| BLOCKED_EXTERNAL | Real YouTube/TikTok/Meta adapters (OAuth + account approval) |
| BLOCKED_EXTERNAL | Live LLM dispatch (provider API keys absent from the API process env) |
| BLOCKED_EXTERNAL | Real STT/TTS/image/video providers |
| BLOCKED_EXTERNAL | Production secrets import, TLS issuance, first public post (owner human gates) |
| NONE (code) | No test could not run for code reasons; nothing was blocked by missing local infrastructure |

---

## 9. Test coverage review (§22)

94.05% statement coverage across 18,594 statements, with 1,838 assertions-of-behaviour passing.

Higher-than-average confidence areas (decision logic is extracted into pure functions and directly tested): budgets/spend authorization, publishing policy evaluation, agent lifecycle transitions, rights gate decisions, SLO evaluation, backup restore evaluation, release freeze, autonomy evidence, remediation safety gate.

**Weak or absent coverage identified** — all also flagged in the gap matrix:

1. **Right-to-publication linkage** — no test exists because no code path exists (the root of finding §7).
2. **Cross-tenant isolation** — no dedicated suite attempting org A's resources as org B.
3. **Authorization/session end-to-end** — identity stores are tested; the full sign-in→capability→denial path is not exercised.
4. **External connector failure modes** — mocks do not simulate upstream 429/5xx/timeout at the adapter boundary.
5. **Frontend** — no automated UI tests at all.
