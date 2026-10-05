# VPS + GitHub Build Runbook

## Recommended filesystem layout
```text
/srv/clipos/
  repo/                 # canonical checkout used by Hermes for integration/state
  worktrees/            # one isolated worktree per active ticket
  runtime/              # non-repo runtime state
  logs/
  cache/
  backups/
  staging/
  production/
```
Adapt paths to the VPS, but keep repo/worktrees/runtime separated.

## Repository bootstrap
1. Create a private GitHub repository `clipos`.
2. Upload/commit this handoff package as the baseline.
3. Protect `main` where your GitHub plan permits it.
4. Clone the repo to the VPS canonical path.
5. Configure least-privilege GitHub credentials for the build identity.
6. Start Hermes from the canonical repo context.
7. Give Hermes `HERMES_START_PROMPT.md`.

## Secrets
Do not put real secrets in Git, Hermes memory, Codex prompts, completion reports or CI logs. Use environment/secret references. `.env.example` contains names/placeholders only.

## Server privilege
Prefer a dedicated non-root OS account for build/orchestration. Use narrowly scoped `sudo` only for explicit infrastructure actions. Application build agents must not receive arbitrary root access simply because Codex/Hermes run on the same VPS.

## GitHub flow
Hermes creates/assigns ticket worktrees. Codex commits within the ticket branch. Required CI and review run before integration. `main` is advanced only by the integration policy.

## Staging / production
During early milestones the VPS may host build tooling and local Docker dependencies. Production ClipOS deployment remains a later milestone. Keep build-time agent credentials separate from ClipOS runtime/provider credentials.
