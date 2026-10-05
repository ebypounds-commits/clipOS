# Temporal Workflow & Automation Specification

## Workflow design principles
- Workflows contain orchestration/state transitions; activities perform side effects.
- Workflow code must be deterministic and replay-safe.
- Activities are idempotent.
- Long waits use durable timers, never process sleep.
- Child workflows isolate platform/media/analytics units.
- Search attributes/business keys permit operator visibility.
- Every workflow carries organization, brand, opportunity/content identifiers, policy version, and correlation ID.

## Primary workflows

### 1. `TrendDiscoveryWorkflow`
Schedule -> collect configured signals -> normalize -> cluster topics -> calculate velocity/saturation/commercial indicators -> persist opportunities -> invoke evaluator -> queue approved candidates.

### 2. `OpportunityEvaluationWorkflow`
Load brand/niche policy -> gather evidence -> score demand, competition, monetization, durability, source availability, rights risk, production cost -> decision: reject/watch/test/produce -> audit decision.

### 3. `SourceAcquisitionWorkflow`
Find permitted source -> verify source identity -> fetch metadata -> create rights review -> ingest authorized media only -> fingerprint -> store provenance.

### 4. `ContentProductionWorkflow`
Rights precheck -> transcription -> semantic segmentation -> candidate extraction -> candidate scoring -> content plan -> hooks/scripts -> asset planning -> media assembly -> render -> QC -> compliance -> produce platform variants.

### 5. `PublishingWorkflow`
Revalidate rights/policy/account health -> budget/rate-limit check -> first-public-post gate -> create idempotency key -> upload -> poll/webhook reconcile -> finalize publication -> schedule metrics collection.

### 6. `AnalyticsCollectionWorkflow`
Collect at configured windows (e.g. early, 24h, 7d, 30d) -> normalize -> update performance snapshot -> cost/revenue attribution -> experiments -> learning.

### 7. `ExperimentWorkflow`
Define hypothesis -> eligibility -> assignment -> produce variants -> publish according to platform-safe schedule -> gather observations -> statistical/business evaluation -> recommend/promote strategy change.

### 8. `RevenueReconciliationWorkflow`
Import provider/affiliate/sponsor revenue -> reconcile identifiers -> append ledger events -> calculate profitability -> flag anomalies.

### 9. `RightsChangeWorkflow`
License expiry/revocation/dispute -> identify dependent assets/publications -> stop scheduled jobs -> quarantine -> create operator/legal review -> remove/modify publications only according to policy and provider capability.

### 10. `IncidentRecoveryWorkflow`
Health/event trigger -> classify -> attempt safe remediation -> verify -> close if recovered; otherwise escalate. Never perform destructive remediation without approval policy.

### 11. `ImprovementProposalWorkflow`
Detect recurring weakness -> evidence packet -> proposal -> classify whether prompt/config/workflow/code change -> run eval sandbox if configuration-only -> create engineering ticket when code required.

### 12. `AgentEvaluationWorkflow`
Load candidate agent/prompt/model config -> run eval suite -> compare baseline -> evaluate quality/cost/latency/safety -> canary recommendation -> promotion or rejection.

## Retry categories
- network/transient: exponential backoff + jitter;
- rate limit: honor provider retry-after and quotas;
- auth expired: refresh once where supported, then operator task;
- invalid request: no blind retries; incident/bug;
- policy rejection: no retry unless configuration/source changes;
- media corruption: quarantine;
- insufficient rights: reject/quarantine.

## Idempotency
Business keys such as:
- `publish:{account_id}:{variant_id}:{scheduled_slot}`
- `render:{project_id}:{render_version}`
- `rights-review:{source_item_id}:{rights_version}`
- `analytics:{publication_id}:{window}`

DB uniqueness plus provider identifiers prevents duplicate side effects.

## Schedules
Temporal schedules trigger trend collection, analytics sync, cost reconciliation, backups/restore verification orchestration, credential health, and system audits. Avoid cron duplication outside Temporal except infrastructure-level liveness tasks.

## Human approvals
Use explicit workflow waiting states. Approval requests contain:
- requested action;
- exact risk;
- evidence;
- expiry;
- approver role;
- what happens on timeout (default deny).
