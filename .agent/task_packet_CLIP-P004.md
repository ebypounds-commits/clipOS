# Codex Task Packet

## Lease
- ticket_id: CLIP-P004
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-P004
- worktree: /srv/clipos/worktrees/CLIP-P004
- lease_expires_at: +30 minutes from dispatch

## Objective
Bounded dry-run to prove the worker loop: write one probe script, execute it, record evidence, produce a schema-valid completion report, commit on this branch. Nothing else.

## Acceptance criteria
- Create `scripts/dryrun_probe.sh` (POSIX sh, executable intent) that prints exactly `DRYRUN_OK` and exits 0.
- Execute it; save its stdout to `.agent/dryrun/probe_output.txt` (exact content `DRYRUN_OK` plus trailing newline).
- Produce `.agent/dryrun/completion_report.json` valid against `schemas/codex_completion_report.schema.json` with:
  ticket_id=`CLIP-P004`, status=`COMPLETE`, branch=`agent/CLIP-P004`,
  worktree=`/srv/clipos/worktrees/CLIP-P004`, summary describing the dry-run,
  files_changed listing your created files, tests=[{name:"dryrun_probe", command:"sh scripts/dryrun_probe.sh", result:"PASS", evidence:".agent/dryrun/probe_output.txt"}],
  commit_shas=<the sha you created>, working_tree_clean=true, blockers=[].
  Validate the JSON parses (python3 -m json.tool).
- Run in the worktree: `python3 scripts/validate_handoff.py` and `python3 scripts/test_agent_lease.py`; both must pass; record both as tests entries.
- Commit everything on branch `agent/CLIP-P004` (git add + git commit, identity already configured). Do NOT push, do NOT touch main, do NOT touch BUILD_STATE.json or 10_TICKETS.yaml.

## Required reading
AGENTS.md, MASTER_CODEX_PROMPT.md, .agent/PLANS.md, schemas/codex_completion_report.schema.json

## Allowed scope
- paths: scripts/dryrun_probe.sh, .agent/dryrun/**, .agent/task_packet_CLIP-P004.md (may remain), git commit on agent/CLIP-P004
- migrations allowed: no
- external network actions allowed: no

## Forbidden actions
- do not merge to main / do not push
- do not select unrelated tickets
- do not expose secrets
- do not change human-gate policy
- do not modify another active worktree
- do not modify BUILD_STATE.json or 10_TICKETS.yaml

## Required gates
- sh scripts/dryrun_probe.sh → DRYRUN_OK, exit 0
- python3 scripts/validate_handoff.py → OK line
- python3 scripts/test_agent_lease.py → RESULT: all passed
- python3 -m json.tool .agent/dryrun/completion_report.json → parses

## Completion output
Write `.agent/dryrun/completion_report.json` per schema, then print a concise human-readable summary.
