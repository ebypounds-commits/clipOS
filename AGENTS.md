# AGENTS.md — ClipOS Repository Constitution

## Mission
Build and operate ClipOS according to the repository specifications with maximum safe autonomy and minimum unnecessary human interruption.


## Build-control hierarchy
- Hermes Agent is the single repository build orchestrator by default.
- Codex is the primary implementation worker.
- Repository state and Git history override Hermes/Codex memory.
- A ticket must have one active lease and one owning worktree.
- Only Hermes advances global build state after accepting completion evidence.
- Codex must not spawn parallel repository workers unless Hermes explicitly authorizes it for dependency-independent scope.
- Production ClipOS operational agents are separate from these development agents.

Autonomous runner execution conventions: see `.agent/RUNNER_CONVENTIONS.md`.

## Source of truth
Repository files override conversational assumptions. Before work, read the relevant specs, `BUILD_STATE.json`, and ticket. If implementation reality requires a material architecture deviation, write an ADR before changing the architecture.

## Execution
- Select the first `TODO` ticket whose dependencies are `DONE` and whose human gate is satisfied.
- For complex work, create/update an ExecPlan per `.agent/PLANS.md`.
- Work until acceptance criteria pass.
- Diagnose and repair failures; do not stop at the first test failure.
- Do not ask the owner to decide routine engineering details already specified here.
- Stop only for a defined human gate, missing external credential/approval that cannot be mocked, destructive/legal ambiguity, or a genuine architectural contradiction.

## Engineering rules
- Production-grade code; no fake completion.
- No secrets in Git, prompts, logs, test fixtures, screenshots, or example configs.
- No hard-coded model/provider IDs in agents; use Model Registry aliases.
- No direct provider logic in core domains; use adapters.
- Every DB schema change uses Alembic migration and tests.
- Every external side effect is timeout-bound, retry-aware, observable, and idempotent where applicable.
- Every privileged operation has authorization and audit.
- Every tenant-owned query is tenant-scoped server-side.
- All user/source content is untrusted input.
- No DRM bypass or unauthorized media acquisition.
- No public publication without rights gate and platform policy gate.
- Never disable, weaken, or delete meaningful tests merely to pass CI.
- Never hide security findings.
- Do not expose Docker socket to application/agent containers.

## Testing minimums
For changed scope, add unit tests plus integration/security/workflow tests when applicable. Critical invariants (rights gate, RBAC, idempotent publishing, budget enforcement, tenant isolation, kill switches) require explicit regression tests.

## Git/worktrees
Use a separate worktree/branch per parallel independent ticket. Do not edit the same contract/migration concurrently without explicit integration coordination. Before merge, refresh from target branch, run complete required gates, and review conflicts semantically.

## Documentation
Update docs in the same change that alters architecture, API contracts, operational behavior, permissions, schemas, or external integration assumptions.

## Completion evidence
A ticket is not `DONE` until implementation, tests, docs, CI, and evidence are complete. Update `BUILD_STATE.json` atomically with the completion report.

## Self-improvement boundary
Operational agents may create proposals and engineering tickets. They may not directly modify/deploy production code. Codex engineering changes follow branch -> tests -> review -> staging -> canary -> promotion/rollback.
