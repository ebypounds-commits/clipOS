# ExecPlan — CLIP-048 hooks/scripts/versioning

## Ticket and objective
CLIP-048 (M11): creative-asset layer — hooks (semantic-key idempotent),
scripts (client id, brief-grounded identity), and append-only
`script_versions` with current_version tracking. No LLM, no provider
identifiers.

## Current repository facts
- Head at plan time: `4bcc577` (CLIP-047 state advance). CI green.
- CLIP-047 landed content_briefs (brief existence is the link gate for
  this ticket). Next free migration: **0029** (0028 → 0029).
- Patterns: semantic-key idempotency (CLIP-042 window key), client natural
  id (CLIP-016/046/047), version append (CLIP-047 plan versions).
- Worktree `/srv/clipos/worktrees/CLIP-048`, branch `agent/CLIP-048`, venv
  Python 3.14.4, lease `mimo-clipos-m1-001`, ttl 14400 from ~11:56Z.

## Scope / non-scope
Scope: `script_hooks.py`, migration 0029 (hooks, scripts,
script_versions), `tests/test_script_hooks.py`, main.py wiring,
test_migrations list, development.md.
Non-scope: CTA variants/offer mappings (later M11+ tickets), hook
selection into plans (engine integration future), rendering, LLM drafting,
web UI.

## Dependencies and assumptions
- Depends on CLIP-047 (DONE). Every hook/script registers against an
  existing brief (404 otherwise). Identical script body re-save is a
  no-op regardless of author (first author of record stays).

## Files/domains expected to change
- New: `clipos_api/script_hooks.py`,
  `migrations/versions/0029_script_hooks.py`,
  `tests/test_script_hooks.py`, `.agent/plans/CLIP-048.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects; org-scoped; no secrets. Audits:
  `hooks.registered`, `scripts.registered`, `scripts.version_appended`.

## Implementation steps
1. Contracts: HookCreate/Hook/Script/ScriptVersionRequest/ScriptVersion.
2. SQL rows: hooks uq (org, brief, text); scripts uq (org, script_id);
   script_versions uq (org, script_id, version).
3. Store: `_brief` existence gate; register_hook (semantic no-op),
   register_script (conflict on different fields), save_version
   (identical-body no-op, else N+1 + current_version bump + audit),
   getters/lists with 404s.
4. Routes (7): hooks register/list, scripts register/get/list,
   versions save/list.
5. Migration 0029 (revises 0028, CREATE-only).
6. Wire main.py; test_migrations += 3 tables; docs section.

## Test/gate plan
- ruff format/check, mypy (CI + py3.12), full pytest SQLite, full pytest
  CI-env, alembic PG 0028 → 0029, OpenAPI probe, secret_scan,
  architecture_guard, state_audit verify.
- TestClient probes: hook+script+version HTTP roundtrip, 404/409 mappings.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed; validators guard state.

## Completion evidence
Ticket, branch, files, migration 0029, gates + results, suite count,
commit SHAs, CI run id.

## Remaining risks / follow-ups
- CLIP-049 (factuality/transformation evals) closes M11.
- Hook → plan integration is future engine work.
- Next free migration: 0030.
