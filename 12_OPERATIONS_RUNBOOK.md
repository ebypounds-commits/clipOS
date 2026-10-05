# ClipOS Operations Runbook

## Daily unattended checks
Automated health workflows verify API, DB, Redis, Temporal, object storage, queues, disk, CPU/RAM, render workers, model providers, publishing connectors, OAuth expiry, quota/rate limits, budget utilization, failed workflows, dead letters, backup freshness, and unexpected public-post volume.

## Alerts requiring immediate stop
- unauthorized publication;
- rights gate bypass evidence;
- secret exposure;
- unexplained budget spike;
- repeated duplicate publishing;
- database integrity failure;
- widespread provider-account restriction;
- malware/security compromise.

The Health Supervisor may activate the narrowest kill switch automatically and create an incident. It may not erase evidence.

## Recovery hierarchy
1. retry transient activity;
2. switch healthy configured model/provider if semantics permit;
3. restart stateless worker/container;
4. pause affected connector/account;
5. revert recent config/prompt version;
6. rollback application deployment;
7. human escalation;
8. disaster restore only when necessary.

## Backup policy baseline
- continuous/WAL or frequent DB backup where feasible;
- daily encrypted full logical/physical backup;
- object storage versioning or replication where feasible;
- configuration export daily;
- weekly automated restore test in isolated environment;
- monthly documented disaster-recovery drill initially.

## Kill switches
`GLOBAL_AUTOMATION`, `PUBLIC_PUBLISHING`, per-brand, per-platform, per-account, per-agent, per-tool, provider/model target, media generation, autonomous-spend.

## Credential expiry
Monitor expiry and refresh status. If refresh fails, stop actions requiring the credential and create one operator task. Do not repeatedly hammer the provider.

## Disk pressure
Media workers stop accepting new heavy jobs before critical disk threshold. Temporary render artifacts are lifecycle-managed; original/provenance assets follow retention policy.

## Incident evidence
Preserve workflow ID, trace ID, agent/model versions, tool calls, provider IDs, relevant logs, content IDs, policy versions, deployment SHA, and timestamps.

## Build-orchestrator operations (VPS)
The software-build control plane has its own health checks: Hermes process/gateway health, Codex availability, Git remote reachability, active ticket leases, worktree cleanliness, disk pressure and stuck worker timeouts. A Hermes outage must not corrupt build state; recovery begins from Git + `BUILD_STATE.json` + ticket/lease evidence.

Do not treat Hermes conversational memory as a recovery database. Preserve useful learned skills, but reconstruct authoritative build position from repository state.
