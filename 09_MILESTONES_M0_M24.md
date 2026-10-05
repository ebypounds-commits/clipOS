# M-1–M24 Autonomous Build Specification

Hermes orchestrates milestone/ticket execution and Codex implements assigned work. A milestone is complete only when its exit gates and all eligible tickets are complete, CI is green, docs are updated, and `BUILD_STATE.json` is advanced under Hermes orchestration control.

## M-1 — VPS / GitHub / Hermes / Codex Preflight
Qualify the actual VPS build environment, GitHub flow, secrets hygiene, Docker, worktrees, orchestration leases, CI bootstrap and Hermes→Codex dispatch/recovery path.

**Exit gates**
- Canonical private-repo baseline exists and remote/branch state is recorded.
- Hermes is proven as single orchestrator and can dispatch a bounded Codex task.
- Codex is proven able to implement/test in an isolated assigned worktree.
- Worktree/lease collision prevention is proven.
- Docker Engine/Compose and baseline runtimes are healthy on the VPS.
- Secret-like tracked-file check passes.
- Baseline CI can execute.
- VPS resource/capacity facts are captured.
- `docs/reports/M-1_COMPLETION.md` exists with evidence.
- `BUILD_STATE.json` points to the first eligible M0 ticket.

## M0 — Repository & Engineering Foundation
Repo layout, package management, Docker dev stack, CI baseline, architecture docs, build-state machinery.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M1 — Identity, Organization & RBAC
Users, organization tenancy, roles/permissions, sessions, service identities, audit foundation.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M2 — Core Persistence, Storage & Observability
PostgreSQL/pgvector, Redis, S3 storage abstraction, OTel/metrics/logging, backup skeleton.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M3 — Temporal Orchestration Foundation
Self-hosted Temporal, worker framework, workflow conventions, idempotency and failure injection tests.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M4 — Agent Registry
Agent definitions/versions, policies, tools, budgets, memory, evaluations, UI/API.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M5 — Model Registry & AI Gateway
Logical aliases, provider adapters, routing, fallback, cost tracking, local-model option.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M6 — Tool, Plugin & MCP Registry
Tool schema/capabilities, MCP/REST adapters, sandbox policy, invocation audit.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M7 — Source, Rights & Provenance
Creators/sources, rights ledger, evidence, provenance manifests, publication hard gate.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M8 — Trend & Niche Intelligence
Signal adapters, topic clustering, opportunity scoring, watchlists, brand fit.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M9 — Ingestion, Transcription & Semantic Processing
Authorized ingestion, media probe, transcripts, segments, semantic metadata.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M10 — Clip Candidate & Opportunity Scoring
Candidate extraction, clip scoring, dedup/fingerprint, review tooling.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M11 — Research, Planning & Script Engine
Research packets, claims/sources, briefs, hooks, scripts, transformation records.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M12 — Automated Media Studio
FFmpeg compositions, captions, audio normalization, aspect variants, deterministic render manifests.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M13 — Voice & Generated Media Adapters
TTS, image/video generation, stock/b-roll adapters, provenance and AI-label metadata.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M14 — Publishing Connectors
YouTube, TikTok, Meta adapter framework, private test mode, status reconciliation.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M15 — Analytics & Normalization
Metric ingestion, normalized performance model, retention snapshots, dashboards.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M16 — Experimentation Engine
Hypotheses, assignments, variants, result evaluation, strategy rules, canary promotion.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M17 — Monetization & Attribution
Affiliate/sponsor/platform revenue models, links, conversion/revenue ingestion.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M18 — Finance & Budget Controls
Cost ledger, budgets, spend policy, profitability, anomaly alerts, reinvestment rules.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M19 — Brand / Channel Factory
Brand templates, launch experiments, quotas, portfolio allocation, lifecycle states.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M20 — Improvement & Agent Factory
Improvement proposals, prompt/agent generation, eval sandbox, promotion policies, engineering-ticket output.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M21 — Self-Healing Operations
Health supervisor, incidents, safe automated remediation, credential/quota monitoring.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M22 — Security, Abuse & Production Hardening
Threat model, penetration/security tests, prompt injection defenses, supply-chain hardening.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M23 — Production Deployment & Disaster Recovery
Hardened Compose, TLS, backups, restore tests, staging/prod, canary/rollback.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.

## M24 — Autonomous Operations Qualification
End-to-end production qualification, guarded public mode, SLOs, operator runbook, autonomy evidence.

**Exit gates**
- Implementation and migration(s) committed where applicable.
- Unit/integration/security tests appropriate to scope pass.
- No new critical/high unresolved security findings introduced.
- Documentation/API contracts updated.
- CI green on milestone head.
- `BUILD_STATE.json` records evidence and next milestone.
