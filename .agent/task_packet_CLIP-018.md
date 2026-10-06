# Codex Task Packet

## Lease
- ticket_id: CLIP-018
- orchestrator_run_id: pending
- branch: agent/CLIP-018
- worktree: /srv/clipos/worktrees/CLIP-018
- lease_expires_at: T+4h from lease

## Objective
Implement Agent Registry schema/API (milestone M4, priority HIGH, depends on CLIP-017).

## Acceptance criteria
- All behavior is covered by appropriate automated tests.
- No provider/model identifier is hard-coded where a registry alias is required.
- External side effects are idempotent and failure-safe where applicable.
- Documentation and BUILD_STATE evidence are updated.
- CI is green.

## Required reading
- 04_AGENT_REGISTRY.md
- 05_MODEL_TOOL_PLUGIN_REGISTRY.md
- 07_SECURITY_GOVERNANCE_RIGHTS.md
- AGENTS.md
- .agent/PLANS.md

## Allowed scope
- paths/domains: services/api (registry module + tests + migrations as required), docs for ADR if needed
- migrations allowed: yes
- external network actions allowed: no

## Forbidden actions
- do not merge to main
- do not select unrelated tickets
- do not expose secrets
- do not change human-gate policy
- do not modify another active worktree

## Required gates
- ruff format --check services/api
- ruff check services/api
- mypy services/api/clipos_api
- pytest -q services/api (coverage >= 60%)
- CI green on GitHub Actions

## Completion output
Produce a report conforming to `schemas/codex_completion_report.schema.json` and a concise human-readable summary.
