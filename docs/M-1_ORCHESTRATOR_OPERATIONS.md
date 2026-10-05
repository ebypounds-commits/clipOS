# M-1 Orchestrator Operations — Hermes single-control binding, leases, restart & overlap

Status: living document (CLIP-P003). Applies to this repository only.

## 1. Hermes binding (single orchestrator, one repo)

- Hermes (profile `youtube-video-content-creator`, run id `hermes-clipos-m1-001`) is the **only** entity authorized to: select tickets, acquire leases, dispatch Codex, merge to `main`, and transition `BUILD_STATE.json` / `10_TICKETS.yaml` state.
- One Hermes session holds **at most one active lease** on this repository at a time.
- Binding rule: before any dispatch, Hermes acquires `.agent/leases/<ticket>.lock` for the ticket it is about to execute. No lease → no worktree, no Codex run, no commit.
- All state transitions happen only in the canonical checkout `/srv/clipos/clipOS` (never inside a worktree).

## 2. Lease format & tool

Tool: `scripts/agent_lease.py` (lease file: `.agent/leases/<ticket>.lock`, git-ignored).

```json
{
  "ticket": "CLIP-P004",
  "owner": "hermes-clipos-m1-001",
  "created_at": "2026-10-05T22:00:00+00:00",
  "expires_at": "2026-10-05T22:15:00+00:00",
  "owner_pid": 1234,
  "state": "active"
}
```

- Default TTL: **900 s** (`lease_timeout_seconds`).
- Commands: `acquire` / `renew` / `release` / `status`. Every mutating command requires `--owner <orchestrator_run_id>`.

## 3. Overlap behavior (two agents want the same ticket)

1. First `acquire` creates the lease atomically (`O_CREAT|O_EXCL`).
2. A second, different owner attempting `acquire` on a **live** lease gets **refused** (`exit 3, error: lease_held`) — there is no silent contention.
3. Worktree rule: a worktree at `/srv/clipos/worktrees/<ticket>` may only be created **after** its lease is held, and is removed at release. A refused agent therefore never starts work.
4. `release` and `renew` refuse `owner_mismatch` (`exit 7`) — an agent can only release/renew its own lease.

## 4. Restart / crash behavior

- If an orchestrator process dies, its lease simply goes **stale** (passes `expires_at`). Nothing is auto-released (a crash must not silently free a ticket another session might be mid-work on).
- A restarting Hermes must:
  1. Run `scripts/agent_lease.py status` — stale leases are flagged `"expired": true`.
  2. Explicitly take over with `acquire <ticket> --owner <new-run-id> --steal-reason "<why>"`.
  3. Taking over a **live** lease (not expired) with `--steal-reason` is also possible, but requires a human-visible reason; the previous owner, previous state and reason are written into the lease file (audited takeover).
  4. Without `--steal-reason`, an expired lease yields `exit 4 (stale_lease_requires_explicit_takeover)` — **stale takeover only ever happens by explicit decision, never automatically.**
- After takeover, the new owner must re-verify repository state (`git status`, `BUILD_STATE.json`, worktree list) before dispatching anything — see `HERMES_START_PROMPT.md`.

## 5. State-transition rule (unchanged, restated for operators)

- `BUILD_STATE.json` / `10_TICKETS.yaml` mutate **only** in the canonical checkout, **only** by Hermes, **only** after gates passed + evidence committed + push verified (`remote HEAD == local HEAD`).
- Validators must pass after every transition: `python3 scripts/validate_handoff.py`.

## 6. Provenance

Implemented and verified under ticket **CLIP-P003** (M-1). Test evidence: `scripts/test_agent_lease.py` output archived in `docs/reports/M-1_COMPLETION.md`.
