# M-1 — VPS, GitHub, Hermes & Codex Preflight

M-1 qualifies the actual environment before application implementation begins. It is intentionally separate from M0.

## Target topology
- GitHub private repository is the source of truth.
- A Linux VPS hosts the working clone, Hermes Agent and Codex.
- Hermes is the **single build orchestrator**.
- Codex is the **primary coding worker**.
- Separate Git worktrees isolate concurrent tickets.
- CI is authoritative for merge qualification.
- Staging and production credentials are not required for M-1.

## Required checks
1. Git repository has a clean baseline commit and remote.
2. Hermes can read the repo and dispatch a bounded coding task.
3. Codex can read/write/test within the repo but cannot silently bypass repository policy.
4. Docker Engine and Compose work on the VPS.
5. Required runtime/tooling baselines are available or can be installed by the build bootstrap.
6. Git worktree creation/removal works.
7. GitHub authentication is least-privilege and push/PR flow works.
8. `.env*`, tokens, SSH keys and provider credentials are excluded from Git.
9. CI baseline can execute from a trivial validation commit/PR.
10. Disk, RAM, CPU and free-space facts are captured for later capacity planning.
11. Time sync, timezone, TLS/DNS plan and outbound network behavior are recorded.
12. A repository lock/lease convention prevents two agents from owning the same ticket/worktree.
13. Hermes and Codex roles are proven with a dry-run ticket.
14. `BUILD_STATE.json` is advanced only under Hermes orchestration lock.

## Exit evidence
Create `docs/reports/M-1_COMPLETION.md` with exact commands/results, versions, remote/branch state, resource snapshot, known limits and the exact first eligible M0 ticket.

M0 may begin only after every critical M-1 gate is PASS or a documented non-blocking deviation is approved by repository policy.
