# Codex Task Packet

## Lease
- ticket_id:
- orchestrator_run_id:
- branch:
- worktree:
- lease_expires_at:

## Objective
<single bounded objective>

## Acceptance criteria
Copy the ticket acceptance criteria verbatim.

## Required reading
List only the relevant repository specs plus `AGENTS.md` and `.agent/PLANS.md`.

## Allowed scope
- paths/domains:
- migrations allowed: yes/no
- external network actions allowed: yes/no

## Forbidden actions
- do not merge to main
- do not select unrelated tickets
- do not expose secrets
- do not change human-gate policy
- do not modify another active worktree

## Required gates
List exact commands/CI lanes expected for this ticket.

## Completion output
Produce a report conforming to `schemas/codex_completion_report.schema.json` and a concise human-readable summary.
