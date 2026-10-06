# Autonomous Runner Conventions

Canonical conventions for how autonomous runners (Hermes orchestrator + Codex workers)
execute tickets in this repository. Derived from `15_HERMES_ORCHESTRATOR.md` and the
executed M-1 procedure in `docs/M-1_ORCHESTRATOR_OPERATIONS.md`. Repository files and
Git history override orchestrator or worker memory.

## Lifecycle

1. **Lease acquire.** Before any work, the orchestrator acquires
   `.agent/leases/<ticket>.lock` via `scripts/agent_lease.py acquire`. One orchestrator
   session holds at most one active lease. No lease -> no worktree, no dispatch, no commit.
2. **Worktree.** The ticket worktree is created only while the lease is held
   (`/srv/clipos/worktrees/<ticket>`, branch `agent/<ticket>`). One ticket owns exactly
   one worktree.
3. **Packet.** The orchestrator assembles a bounded task packet (template:
   `.agent/CODEX_TASK_PACKET_TEMPLATE.md`) and validates it with
   `scripts/validate_packet.py` before dispatch.
4. **Dispatch.** Codex executes only the packet scope, runs the required gates, and
   produces a structured completion report (schema:
   `schemas/codex_completion_report.schema.json`).
5. **Ground truth.** Verification uses only observed evidence (session/tool outputs, CI
   runs, file contents). See below.
6. **Verify.** The orchestrator re-runs the packet's required gates on the produced
   change set (or re-runs them outside a restricted sandbox where the worker was
   network/pip/container-blocked; SKIPs must be listed per gate with a reason).
7. **Land.** With all gates green, the branch is merged per repo policy. `BUILD_STATE.json`
   and `10_TICKETS.yaml` mutate only in the canonical checkout, only by Hermes, only after
   gates passed and evidence is committed/pushed.
8. **CI green.** `BUILD_STATE.json` must never advance past a red `baseline-ci` run on the
   pushed SHA (status via the `ci-results` write-back matching the pushed sha).
9. **Evidence.** The completion report cites verifiable facts: commit sha, CI run id,
   test counts, command exit codes. Model-claimed output is never evidence.
10. **State advance.** `validate_handoff.py` + `validate_state.py` pass after every state
    mutation; the state advance is committed atomically.
11. **Lease release.** The lease is released (`agent_lease.py release --owner ...`) after
    the state advance; the worktree is removed at release per M-1 procedure.

## Single-lease exclusivity

A ticket has exactly one active lease and one owning worktree. A second owner's `acquire`
on a live lease fails (`lease_held`, exit 3). Stale leases are never freed automatically;
takeover requires an explicit `--steal-reason` and is audited in the lease file. Workers
never dispatch parallel repository workers unless Hermes explicitly authorizes
dependency-independent scope.

## Ground-truth rule

Only the following count as evidence of completion or failure:

- observed session/function outputs captured in the completion report (command, result);
- orchestrator-gated re-runs outside a restricted sandbox (CI runs, tool exits);
- verifiable repository facts (commit shas, file contents, `git status`).

Model-claimed output — a runner asserting a test passed without an observed run — is
never evidence. Anything not runnable in the worker's environment is reported as
`SKIP` with the concrete reason (e.g. no network, no docker, no pytest); it is never
reported as PASS.

## Dispatch failure signatures and retry policy

- Transient tool/provider failure: retry with backoff, keyed by the failure artifact
  (gate id + output artifact), max 3 attempts per ticket.
- Implementation failure: return diagnostics to the same ticket/worktree; a fresh repair
  attempt requires the failure evidence attached to the retry packet.
- Success is artifact-keyed: a retry succeeds only when the previously failing gate
  produces a new observed pass for the same artifact key; repeated identical failures are
  not retried past the attempt cap.
- Merge conflict: integration/rebase task in the same ticket context.
- Environment failure: classify as an ops incident; do not change application code blindly.
- Human gate: stop that ticket, notify the operator once, keep independent work moving.

## Escalation to human gates

The runner must stop and escalate (operator notification, ticket blocked) for:
missing external secrets or credentials that cannot be mocked; cost/spend decisions
beyond configured budget; destructive or irreversible operations; and any requested
suppression of a real failure (weakened/deleted tests, silenced findings) — suppression
requires an ADR and evidence, never a runner decision.

## State-mutation rules

- `BUILD_STATE.json` / `10_TICKETS.yaml` mutate only in the canonical checkout, only by
  Hermes, only after gates passed, evidence committed and push verified.
- After **every** mutation: `python3 scripts/validate_handoff.py` and
  `python3 scripts/validate_state.py` must pass; otherwise roll back.
- Evidence fields must cite verifiable facts (commit sha, CI run id, test counts);
  placeholders or unobserved claims are invalid.
- Material state transitions are recorded in the append-only
  `docs/reports/STATE_AUDIT.jsonl` via `scripts/state_audit.py` (append is idempotent per
  ticket; existing lines are never rewritten). `state_audit.py verify` must pass before
  state advances are considered complete.
