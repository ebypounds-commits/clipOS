# Transfer / Start on VPS with Hermes + Codex

## Preferred path
1. Create a private GitHub repo `clipos`.
2. Extract this package to a local folder and commit it as the baseline, or upload it as the repo baseline.
3. Clone the GitHub repo into the VPS canonical path (recommended `/srv/clipos/repo`, adaptable).
4. Ensure Hermes and Codex can access the canonical checkout under a dedicated build user.
5. Do not give either agent real ClipOS production secrets at this stage.
6. Start Hermes in the canonical repo and provide `HERMES_START_PROMPT.md`.
7. Hermes begins M-1, creates worktrees, and dispatches Codex using `MASTER_CODEX_PROMPT.md`.

## Important
Do not separately start an autonomous Codex loop that also selects tickets while Hermes is orchestrating. That would create two schedulers. Codex may be opened interactively for inspection, but repository-changing autonomous work should be leased through Hermes unless the operator deliberately pauses Hermes ownership first.

## Recovery
If the VPS/Hermes session restarts, recover by reading Git status/history, `BUILD_STATE.json`, `10_TICKETS.yaml`, active `.agent/locks/` records and latest completion reports before redispatching work.
