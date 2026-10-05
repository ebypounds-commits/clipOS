# ClipOS Product Requirements Document

## 1. Product vision
ClipOS is a self-hosted autonomous media-business platform that can operate a portfolio of content brands with minimal routine human intervention. It should continuously discover opportunities, produce rights-safe content, distribute it, attribute revenue/cost, and improve future decisions.

The platform is not a reposting farm. Its durable business model is built on owned media, licensed media, public-domain media, explicit creator partnerships, generated media, and materially transformed media with substantial original value.

## 2. Primary outcomes
- Produce platform-ready short-form and derivative media from approved source material and original research.
- Maintain a complete provenance/rights chain for every published asset.
- Publish through supported official APIs with retry and status reconciliation.
- Operate an Agent Registry where each specialist has configurable model aliases, tools, budgets, memory, policies, fallbacks, and evaluations.
- Track content economics: cost, revenue, affiliate conversion, sponsor attribution, RPM-like metrics, profit per asset, and profit per brand.
- Run controlled experiments and automatically adjust future allocation within policy limits.
- Detect failures and recover automatically where safe.
- Convert repeatable product/production problems into structured improvement proposals and Codex engineering tickets.

## 3. Personas
### Owner / Super Admin
Controls organizations, secrets, global budgets, platform connections, production gates, and irreversible actions.

### Operator
Can configure brands, campaigns, agent policies, schedules, and review queues within granted permissions.

### Reviewer
Can approve rights, content, publishing, sponsorship, or exceptional-risk decisions.

### Service Agent
Machine identity with least-privilege capabilities.

## 4. Functional domains
### A. Control Plane
Organizations/workspaces, users, roles, API/service identities, settings, feature flags, audit events, approvals, budgets, kill switches.

### B. Brand & Portfolio
Brand profiles, niche thesis, target audience, tone, forbidden topics, visual identity, platform accounts, content objectives, commercial objectives, production quotas.

### C. Trend & Opportunity Intelligence
Signals, trend observations, topic entities, competitive saturation, commercial intent, source availability, rights risk, opportunity scoring, watchlists, experiments.

### D. Source & Rights
Creator/source records, owned/licensed/public-domain/generated/transformed classifications, licenses, allowed platforms, territories, commercial-use rights, expiry, attribution, evidence documents, rights review state.

### E. Content Planning
Ideas, briefs, source packets, research packets, content plans, hooks, scripts, calls to action, product/offer mapping.

### F. Media Production
Assets, transcripts, segments, candidate clips, voiceovers, captions, graphics, b-roll, render projects, platform variants, thumbnails, QC reports.

### G. Distribution
Platform accounts, publishing jobs, schedules, metadata, uploads, processing reconciliation, publication status, retries, removals, edits where supported.

### H. Analytics & Learning
Native metrics, normalized metrics, retention snapshots, comments/signals, experiments, cohorts, content features, learned rules, prompt/model performance.

### I. Monetization & Finance
Platform revenue, affiliate clicks/conversions, sponsor campaigns, lead-generation events, product revenue, cost ledger, budgets, profitability, reserve/reinvestment policies.

### J. Agent Platform
Agent Registry, Prompt Registry, Model Registry, Tool Registry, Plugin/MCP Registry, policies, memory namespaces, evaluations, traces, run costs, agent versions.

### K. Operations
Workflow state, incidents, retries, dead-letter jobs, notifications, backups, restore tests, service health, credential expiry, platform quota/rate-limit monitoring.

## 5. Core user journeys
### Journey 1 — Configure an agent
Owner selects agent -> assigns logical model alias -> allowed tools -> budgets -> memory -> evaluator -> fallback chain -> saves version -> canary evaluation -> activation.

### Journey 2 — Discover and produce
Trend workflow finds candidate -> opportunity scored -> source/rights packet assembled -> content plan created -> script produced -> media rendered -> QC/compliance evaluated -> queued for platform variants.

### Journey 3 — Publish
Publishing workflow verifies rights=clear, account healthy, budget available, content approved, first-public-post gate passed -> uploads -> reconciles provider processing -> stores external IDs -> schedules analytics collection.

### Journey 4 — Learn
Metrics arrive -> normalized -> experiment assignments resolved -> economics calculated -> learning records created -> future content allocation adjusted inside configured bounds.

### Journey 5 — Self-improve safely
Agent/workflow detects recurring weakness -> creates improvement proposal -> evaluation design generated -> optional engineering ticket generated -> Codex implements in branch/worktree -> tests/security/evals -> staging/canary -> promote or rollback.

## 6. Critical product rules
- `rights_status=CLEAR` is mandatory before public publication.
- No secret values in database rows, logs, traces, prompts, or Git history; store secret references only.
- Every external write action has idempotency keys and an audit event.
- Every workflow must survive process restarts.
- Every provider call has timeout, bounded retries, circuit breaker, rate-limit handling, and observability.
- Any agent/tool/action can be globally disabled without redeploying.
- Platform accounts have per-account publishing caps below provider maximums.
- Costs are recorded per workflow, run, agent, provider, brand, and asset where possible.
- Public posting is disabled by default in new environments.

## 7. Autonomous operating modes
- `OBSERVE`: discover/measure only; no creation or publishing.
- `DRAFT`: create content but never publish.
- `PRIVATE_TEST`: publish only private/unlisted/test destinations where supported.
- `GUARDED_PUBLIC`: public publishing allowed within explicit brand/platform policies; exceptional conditions require approval.
- `AUTONOMOUS_PUBLIC`: routine public publishing within strict budgets/limits; human gates remain for legal, financial, platform, and destructive actions.

The system must launch in `DRAFT`, then `PRIVATE_TEST`, then `GUARDED_PUBLIC`. `AUTONOMOUS_PUBLIC` requires production-readiness evidence.

## 8. Non-functional requirements
### Reliability
- no single failed external provider should corrupt workflow state;
- at-least-once activity execution with application-level idempotency;
- durable workflow orchestration;
- automatic retry for transient failures;
- dead-letter/incident path for permanent failures.

### Performance
- API p95 target under 500 ms for ordinary control-plane reads/writes excluding external calls;
- rendering isolated from API processes;
- large media never proxied through application memory unnecessarily;
- resumable uploads where provider supports them.

### Security
- least privilege RBAC/ABAC;
- CSRF/session protection;
- encrypted transport;
- encrypted backups;
- secrets manager;
- dependency/container scanning;
- audit trail for privileged actions.

### Maintainability
- modular monolith for business domains initially;
- separate scalable workers for media/AI/background execution;
- stable internal contracts/events;
- adapters around external providers;
- no provider-specific logic in core domain models.

### Portability
- Dockerized;
- self-host on Linux;
- S3-compatible object storage;
- PostgreSQL-compatible core persistence;
- provider/model adapters replaceable by configuration.

## 9. Success metrics
- percentage of workflows completed without human intervention;
- publication success rate;
- rights/compliance rejection rate;
- cost per produced/published asset;
- gross and net revenue by brand;
- profit per 1,000 impressions;
- profit per content unit;
- model cost per accepted output;
- experiment velocity;
- mean time to recovery;
- percentage of incidents auto-recovered;
- zero unauthorized publication events.

## 10. MVP production proof
A successful initial proof must:
1. create/configure at least 10 agents via the registry;
2. use at least two LLM provider adapters plus one fallback path (real or mocked in CI);
3. ingest authorized/original media;
4. transcribe and identify candidate clips;
5. render vertical video with captions;
6. run rights/QC/compliance gates;
7. publish to at least one real platform in private/unlisted mode;
8. reconcile processing status;
9. collect analytics;
10. attribute costs;
11. run one A/B experiment;
12. recover one intentionally injected worker failure;
13. restore from a backup in staging;
14. demonstrate complete audit trace from source to publication.
