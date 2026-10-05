# ClipOS Domain & Data Model

## Design rules
- UUID/ULID primary keys.
- `organization_id` on tenant-owned rows.
- UTC timestamps.
- optimistic concurrency/version columns for mutable aggregates where stale writes matter.
- soft-delete only where business/legal recovery requires it; otherwise explicit archive state.
- append-only audit and finance events.
- database enums should be used sparingly; prefer constrained text/reference tables when evolution is expected.

## Core domains and tables

### Identity & Governance
`users`, `organizations`, `memberships`, `roles`, `permissions`, `role_permissions`, `service_identities`, `sessions`, `api_credentials` (references only), `approvals`, `approval_steps`, `audit_events`, `feature_flags`, `kill_switches`, `policy_sets`, `policy_versions`.

### Brands & Portfolio
`brands`, `brand_profiles`, `brand_guidelines`, `brand_platform_accounts`, `niche_theses`, `audience_profiles`, `content_objectives`, `commercial_objectives`, `production_quotas`, `portfolio_allocations`.

### Signals & Opportunity
`signal_sources`, `signal_observations`, `topics`, `topic_aliases`, `trend_snapshots`, `competitor_entities`, `opportunities`, `opportunity_scores`, `watchlists`, `opportunity_decisions`.

### Rights & Provenance
`creators`, `content_sources`, `source_items`, `rights_records`, `licenses`, `license_evidence`, `attribution_requirements`, `rights_reviews`, `rights_events`.

`rights_records.status`: `UNKNOWN | REVIEW_REQUIRED | CLEAR | RESTRICTED | EXPIRED | REVOKED | REJECTED`.

`rights_records.basis`: `OWNED | LICENSED | CREATOR_PARTNERSHIP | PUBLIC_DOMAIN | PERMISSIVE_LICENSE | GENERATED | TRANSFORMATIVE_REVIEW`.

### Research & Planning
`research_jobs`, `research_packets`, `claims`, `claim_sources`, `content_ideas`, `content_briefs`, `content_plans`, `hooks`, `scripts`, `script_versions`, `cta_variants`, `offer_mappings`.

### Media
`media_assets`, `asset_versions`, `transcripts`, `transcript_segments`, `clip_candidates`, `clip_scores`, `caption_tracks`, `voice_assets`, `image_assets`, `render_projects`, `render_jobs`, `render_outputs`, `platform_variants`, `qc_reports`, `content_fingerprints`.

### Publishing
`platforms`, `platform_accounts`, `platform_capabilities`, `publish_policies`, `publication_jobs`, `publications`, `publication_status_events`, `provider_webhook_events`, `publishing_idempotency`.

### Analytics
`metric_definitions`, `metric_samples`, `retention_samples`, `audience_snapshots`, `engagement_events`, `comment_signals`, `analytics_sync_runs`, `normalized_performance_snapshots`.

### Monetization
`revenue_sources`, `revenue_events`, `affiliate_programs`, `affiliate_offers`, `affiliate_links`, `conversion_events`, `sponsor_accounts`, `sponsor_campaigns`, `lead_events`, `product_offers`.

### Finance
`cost_events`, `budgets`, `budget_periods`, `spend_authorizations`, `financial_ledger_events`, `profitability_snapshots`, `reinvestment_policies`.

### Experiments & Learning
`experiments`, `experiment_variants`, `assignments`, `experiment_observations`, `experiment_results`, `learning_records`, `strategy_rules`, `strategy_rule_versions`.

### Agent Platform
`agents`, `agent_versions`, `agent_model_policies`, `agent_tool_grants`, `agent_memory_policies`, `prompts`, `prompt_versions`, `agent_runs`, `agent_run_steps`, `agent_costs`, `agent_evaluators`, `eval_suites`, `eval_cases`, `eval_runs`, `eval_results`.

### Models
`model_providers`, `model_endpoints`, `model_aliases`, `model_alias_targets`, `model_health_samples`, `model_price_cards`, `model_capabilities`, `provider_rate_limits`.

### Tools & Plugins
`tools`, `tool_versions`, `tool_permissions`, `plugins`, `plugin_versions`, `mcp_servers`, `connector_accounts`, `webhook_endpoints`, `webhook_deliveries`, `tool_invocations`.

### Workflows & Operations
`workflow_definitions`, `workflow_versions`, `workflow_runs`, `workflow_business_keys`, `incidents`, `incident_events`, `dead_letter_items`, `health_checks`, `backup_runs`, `restore_tests`, `notifications`, `operator_tasks`.

## Critical relationships
- A `publication` references exactly one `platform_variant` and one `rights_record` snapshot/version.
- A `platform_variant` derives from a `render_output` and `content_plan`.
- A `render_output` references all constituent media assets for provenance.
- A `content_source` may have multiple source items and rights records over time.
- Rights revocation creates an event and triggers publication review/removal workflow where applicable.
- Every `agent_run` references the exact `agent_version`, `prompt_version`, logical model alias, actual provider/model selected, tools invoked, token/cost usage, and trace identifiers.
- Every revenue/cost event is append-only and can be reconciled to provider evidence.

## Indexing priorities
- `(organization_id, created_at)` on tenant-owned operational tables;
- workflow business keys unique within organization;
- external provider IDs unique per account/platform;
- `rights_records(source_item_id, status)`;
- `publications(platform_account_id, external_id)`;
- analytics partitioning by date when volume justifies it;
- pgvector indexes on semantic memory/research embeddings only after measured query needs.

## Retention
Define retention policy classes:
- operational logs: configurable, default 30–90 days;
- audit/security logs: longer retention;
- raw provider webhooks: short-to-medium retention after normalization;
- rights evidence: retained for life of dependent publications plus legal retention policy;
- model prompts/outputs: configurable, with redaction and privacy controls;
- media masters: policy-driven tiering/archive.
