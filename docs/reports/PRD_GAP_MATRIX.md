# ClipOS — PRD Gap Matrix

**Audit date**: 2026-10-20 · **Commit**: `be4acd2` · **Method**: code + live-runtime evidence

Status vocabulary (per audit §7): `BUILT` · `PARTIAL` · `STUB_ONLY` · `BROKEN` · `MISSING` · `BLOCKED_ENVIRONMENT` · `BLOCKED_EXTERNAL` · `UNVERIFIED`

---

## A. Capability matrix by PRD domain

Completion % is derived from: (routers implemented ÷ stated capability), verified by tests, minus anything mock-only or unenforced. It is a judgement anchored to the evidence below, not a line count.

| # | Domain | % | Status | What works | What does not | Evidence | Priority |
|---|---|---|---|---|---|---|---|
| 1 | Foundation | 95 | BUILT | API, migrations, dev stack, CI, state machine | — | 13/13 gates; `/health` v1.0.0 | — |
| 2 | Identity / Tenancy / RBAC | 70 | PARTIAL | Orgs, memberships, sessions, service identities (in-memory + DB stores), capability model | No end-to-end auth path exercised; no cross-tenant isolation suite | `identity.py`, `tenancy.py`, `authorization.py`; no auth E2E test | P1 |
| 3 | Data / Storage / Infrastructure | 92 | BUILT | Postgres+pgvector, Redis, MinIO, compose topologies, hardening invariants | — | `deploy_topology_check` PASS; 137 tables model↔DB agree | — |
| 4 | Temporal / Durable Workflows | 75 | PARTIAL | Client config, workflow framework, task queues, reachability | Scheduled jobs declared but not scheduled | `temporal_config.py`, `workflow_framework.py`; `/operator/health` shows temporal_reachable=true | P1 |
| 5 | Agent Registry | 95 | BUILT | Register, version, lifecycle DRAFT→EVALUATING→CANARY→ACTIVE, eval gate, policy, budgets | — | `agent_registry.py`; FK-ordering bug found and fixed; live activated `demo-research` | — |
| 6 | Model Registry / LLM Gateway | 92 | BUILT | 10 routes / 6 aliases, fallback chain, resolution, health, cost, dispatch, 3 wire protocols | Live calls await provider keys | `registry_audit.py`; dispatch names exact missing env var | — |
| 7 | Tool / Plugin / MCP Registry | 80 | PARTIAL | Registry, policy, permissions, plugin registry, MCP boundary | Sandboxing is policy-level; no execution isolation | `tool_registry.py`, `tool_policy.py`, `plugin_registry.py`, `mcp_boundary.py` | P1 |
| 8 | Rights / Provenance | **35** | **BROKEN** | Rights ledger, entities, provenance, gate evaluate + tests | **Gate not enforced on publication** | Live: uncleared item published SUCCEEDED; gate says 404 | **P0** |
| 9 | Trend Intelligence | 85 | BUILT | Signal sources, topics, opportunities, watchlists, collection schedules, scoring | — | routers + tests | P2 |
| 10 | Media Ingestion | 85 | BUILT | Ingest, metadata, ffprobe validation, quarantine, path confinement | — | `media_ingestion.py`, `asset_validation.py` | — |
| 11 | Transcription / Semantic | 65 | STUB_ONLY (adapter) | Transcript/segment persistence, semantic search, embeddings | Real STT provider is a mock | `transcription.py` `MockTranscriptionAdapter` | P1 |
| 12 | Clip Extraction / Scoring | 90 | BUILT | Segmentation, candidates, scoring, dedup, review | — | `clip_candidates*`, `clip_scores.py`, `clip_dedup.py` | — |
| 13 | Creative / Script Pipeline | 90 | BUILT | Research packets, briefs, hooks, scripts, versions, factuality/transformation evals | — | 5 planning stages pass E2E | — |
| 14 | Rendering / FFmpeg | 90 | BUILT | Render projects, jobs, real ffmpeg subprocess (`shell=False`, argv), captions, variants, QC | — | `render_pipeline.py`, `render_variants.py` | — |
| 15 | Voice / Generated Media | 55 | STUB_ONLY (adapters) | Voice assets, TTS/image/video adapters registered | Real providers are mocks; local adapter exists | `voice_assets.py`, `generation_adapters.py`, `local_adapter.py` | P1 |
| 16 | Publishing | 70 | PARTIAL | Policy, modes, owner gate (enforced), kill switch (enforced), idempotency, job tracking | Platform adapters are mocks; **rights not consulted** | 6/6 private-mode invariants pass; adapters explicitly mock | P0/P1 |
| 17 | Analytics | 80 | PARTIAL | Collect, dashboard, snapshots, metric reconciliation | Aggregation is mock-source only | `analytics*.py`, `performance_snapshots.py` | P2 |
| 18 | Experimentation | 90 | BUILT | Experiments, variants, assignment, evaluation, orchestration | — | 5 experiment stages pass E2E | — |
| 19 | Monetization | 80 | PARTIAL | Revenue ledger, attribution, affiliate, sponsorship, reconciliation, profitability | Mock revenue source | `revenue_ledger.py` `MockRevenueAdapter` | P2 |
| 20 | Finance / Budgeting | 88 | BUILT | Cost ledger, budgets, spend authorization (fail-closed), finance controls, quotas, cost anomalies | — | `budgets.py` `authorize_spend` verified two-directionally | — |
| 21 | Channel Factory | 85 | BUILT | Brands, channel templates, brand lifecycle, shutdown policies | — | `brand_*.py`, `incubation.py` | — |
| 22 | Agent Factory / Self-Improvement | 85 | BUILT | Improvement proposals, evidence, engineering tickets, eval sandbox, gated promotion | — | `improvement.py`, `codex_tickets.py`, `eval_sandbox.py` | — |
| 23 | Observability / Self-Healing | 70 | PARTIAL | Audit trail, operator health, health supervisor signals, incidents, remediation, monitors | Metrics/traces thin; alert delivery unwired | `health_supervisor.py`, `monitors.py`, `incidents.py` | P1 |
| 24 | Security Hardening | 78 | PARTIAL | Injection guard, threat model, secret scan, RBAC model, rate limiting, path confinement, no `shell=True` | Rights bypass (see 8); no cross-tenant suite | `injection_guard.py`, `threat_model.py`; 11/11 qual controls | P0/P1 |
| 25 | Deployment | 80 | BUILT | Hardened staging+prod compose, canary/rollback, release evidence, bootstrap validation | Never deployed for real (human gates) | `deploy/`, `deployments.py`, `production_bootstrap.py`, `release/v1.manifest.json` | P1 |
| 26 | Autonomous Operations | 85 | BUILT | SLO objectives, cost-cap verification, drill records, autonomy evidence, ops manual | Production SLO measurements need live traffic | `slo_qualification.py`, `autonomy_evidence.py`, `docs/OPERATIONS_MANUAL.md` | P1 |

**Tally**: BUILT 15 · PARTIAL 9 · STUB_ONLY 2 (adapters, by design) · BROKEN 1 · MISSING 0 · BLOCKED_EXTERNAL (cross-cutting: real platform/LLM/STT/TTS credentials)

---

## B. Ticket ledger — verification result

All 108 tickets are marked `DONE` in both `10_TICKETS.yaml` and `BUILD_STATE.json`, with **exact agreement** (no YAML-DONE/state mismatch in either direction). Each ticket carries acceptance criteria; exactly one (`CLIP-099`) is human-gated.

Rather than restate 108 rows, here is the verification method and its outcome:

| Verification step | Result |
|---|---|
| Implementation located for each ticket's domain | 112 modules cover all 26 milestones' scopes |
| Tests located | 121 test files; **0 modules unreferenced by any test** |
| Acceptance criteria present | 108/108 tickets |
| CI evidence | green at `be4acd2` (validate, test, quality, build) |
| Stub-only implementation | 18 mock/in-memory classes — all in adapter/store positions, all labelled in docstrings; none in decision logic |
| `TODO`/`FIXME` in product code | **0** |
| `NotImplementedError` | 12, all in `providers.py` (abstract protocol methods — legitimate) |

**No ticket was downgraded for missing code.** The one substantive downgrade is at the *capability* level, not the ticket level: CLIP-0xx implemented the rights gate as a service and tested it, which satisfies the letter of its acceptance criteria, while the end-to-end enforcement path is incomplete. That is recorded above as domain 8 `BROKEN`.

---

## C. False-completion and stub detection (§8)

Context-inspected results:

| Indicator | Count | Assessment |
|---|---|---|
| `TODO` / `FIXME` in `clipos_api/` | 0 | clean |
| `NotImplementedError` | 12 | `providers.py` Protocol stubs — correct design |
| `pass`-only bodies | 3 | `clip_candidates.py:194`, `health_supervisor.py:206`, `worker.py:101` — exception-handler no-ops, verified benign |
| `placeholder` | 4 | all docstring prose (reference/command templates) |
| `coming soon` | 0 | — |
| Mock/Stub/Fake/InMemory classes | 18 | **All in adapter or in-memory-store positions**, each documented. None in a decision path. |

**UI-without-backend**: none found. `STATIC_MOCK_UI` classified for none of the 5 pages — all fetch live API data.

**Persistence mismatch**: none. Every PRD-durable domain writes to PostgreSQL; `InMemory*` stores are explicitly test/dev substitutes with DB-backed siblings.

---

## D. Build-state integrity (§28)

Contradictions found — **one minor**:

1. **`M-1` missing from `completed_milestones`.** Tickets reference milestone `M-1` (the preflight/P001–P006 tickets), and `completed_milestones` lists M0–M24 (25 entries) but not `M-1`. The preflight work is demonstrably done (`docs/reports/M-1_COMPLETION.md`, the P-tickets, the whole build). Proposed correction: add `"M-1"` to `completed_milestones` — **not applied**, per §28 ("list proposed corrections first").

Everything else reconciles: counters, ledgers, audit coverage (108/108), current ticket/status in the terminal state (`NONE` / `M24_COMPLETE`), and CI green on the frozen commit.

---

## E. Longest functional end-to-end path

Verified working, end-to-end, today (28 steps, 0 failures):

```
research packet → claim → verification → content brief → plan
→ hooks → script → script version
→ video generation (mock adapter) → transcript (mock adapter)
→ clip candidate extraction
→ render project
→ platform account → publish policy → private publication  [ALLOWED]
→ analytics collect → dashboard → performance snapshot
→ revenue source → sync → dashboard → profitability
→ experiment → variants → activate → assignment
```

**Working.** The private-mode invariants hold throughout: OBSERVE denies all, PRIVATE_TEST cannot publish PUBLIC, PUBLIC refused while `public_enabled=false`, kill switch denies, no secret material leaks.

The path **breaks** at the rights step — or rather, the rights step is *skipped*: the pipeline above published `storage://qual-video` with no rights record and was not blocked. In a compliant system that step must gate the publish; today it does not.
