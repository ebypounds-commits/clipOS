# Hermes Orchestrator Architecture

## Role split
Hermes Agent is the persistent **control-plane orchestrator** for the software build. Codex is the **engineering execution engine**. ClipOS production agents are a third, separate runtime and must not be confused with build agents.

### Hermes owns
- reading `BUILD_STATE.json` and `10_TICKETS.yaml`;
- choosing eligible tickets;
- acquiring/releasing ticket leases;
- creating or assigning Git worktrees/branches;
- assembling bounded Codex task packets;
- dispatching Codex;
- supervising timeouts/retries;
- requesting independent review;
- integrating only qualified changes;
- advancing ticket/build state under lock;
- scheduling follow-up work;
- preserving orchestration memory/skills without making memory a source of truth;
- operator notifications for real human gates.

### Codex owns
- inspecting assigned scope;
- creating/updating the ExecPlan;
- implementing code/docs/migrations for the assigned ticket;
- running tests and repairing failures;
- self-reviewing the diff;
- producing structured completion evidence;
- committing changes on its assigned branch when permitted.

### Codex does not own by default
- selecting unrelated next tickets;
- assigning parallel worktrees;
- merging to protected `main`;
- changing human-gate policy;
- editing other active agents' branches/worktrees;
- mutating `BUILD_STATE.json` outside the Hermes-controlled completion transaction;
- production credential creation;
- unrestricted production deployment.

## Single-orchestrator rule
Only Hermes may dispatch concurrent repository workers unless `orchestration.codex_subagents=true` is explicitly enabled. This prevents double orchestration and branch collisions.

## Ticket lease protocol
A lease record should contain:
- ticket id;
- orchestrator run/session id;
- branch;
- worktree path;
- assigned worker;
- acquired timestamp;
- heartbeat timestamp;
- lease expiry;
- status.

Use `.agent/locks/` for local lease metadata and/or a small durable Hermes state store. A stale lease may be reclaimed only after verifying no live worker owns the worktree and preserving its uncommitted changes.

## Branch/worktree convention
Recommended branch: `agent/<ticket-id>-<short-slug>`.
Recommended VPS worktree root: `/srv/clipos/worktrees/<ticket-id>/` or an operator-selected equivalent outside the primary checkout.

One ticket owns one worktree. Shared generated state, caches and secrets must not be committed into worktrees.

## Dispatch packet
Each Codex task packet contains only:
- ticket id and acceptance criteria;
- exact branch/worktree;
- relevant specs;
- required gates;
- current dependency evidence;
- explicit forbidden actions;
- completion-report schema.

## Completion transaction
Hermes accepts a ticket only when:
1. assigned branch is clean;
2. acceptance criteria have evidence;
3. required CI/tests are green;
4. independent review requirements are satisfied;
5. security/rights/budget gates are satisfied;
6. integration does not conflict with newer `main`;
7. completion report exists.

Then Hermes merges/integrates according to repo policy, updates `10_TICKETS.yaml` and `BUILD_STATE.json` atomically, commits that state update, and selects the next ticket.

## Failure policy
- transient tool/provider failure: retry with backoff;
- Codex implementation failure: return diagnostics to same ticket/worktree;
- repeated failure: request a fresh Codex repair attempt with failure evidence;
- merge conflict: integration/rebase task in the same ticket context;
- environment failure: classify as M-1/ops incident rather than changing application code blindly;
- human gate: keep all other independent work moving and notify operator once.

## Hermes capabilities used
The design assumes the current Hermes Agent model of persistent memory/skills, terminal/file tooling, task delegation, scheduled jobs and MCP extensibility. Treat these as orchestration aids; repository files and Git history remain authoritative.
