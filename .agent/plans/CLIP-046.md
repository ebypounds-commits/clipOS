# ExecPlan — CLIP-046 research packet and claim/source model

## Ticket and objective
CLIP-046 (M11 opener): persistence + API for research packets, claims, and
claim sources per `03_DOMAIN_DATA_MODEL.md` Planning group and PRD domain
E. Idempotent registration, audited veracity lifecycle, refs-only sources.
No LLM, no provider identifiers.

## Current repository facts
- Head at plan time: `c7b472f` (CLIP-045 state advance, M10 closed). CI green.
- Next free migration: **0027** (chain ...0026 → 0027).
- Patterns: CLIP-016 opportunity registration (client natural id, idempotent
  per (org, id), conflict on different fields), decision audits with
  actor/reason refs (CLIP-036/045), routes providers-before-decorators with
  `_database_engine` late import (CLIP-033).
- Worktree `/srv/clipos/worktrees/CLIP-046`, branch `agent/CLIP-046`, venv
  Python 3.14.4, lease `mimo-clipos-m1-001` expires 2026-10-12T15:25Z.

## Scope / non-scope
Scope: `research.py`, migration 0027 (3 tables), `tests/test_research.py`,
main.py wiring, test_migrations list, development.md section.
Non-scope: `research_jobs` (execution records — not in ticket title),
brief/content-plan engine (CLIP-047), factuality evaluations (CLIP-049),
source crawling/content fetching (refs only), web UI, LLM extraction.

## Dependencies and assumptions
- Depends on CLIP-045 (DONE). Optional `topic_id` is a soft reference to
  M8 topics (no FK, no existence check — refs-not-blobs convention).
- Claim creation always starts UNVERIFIED; verification must use the
  audited veracity route (auditability over convenience).
- `source_ref` capped at 512 chars so the (claim_id, source_ref) unique
  index stays under the PG btree entry limit.

## Files/domains expected to change
- New: `clipos_api/research.py`,
  `migrations/versions/0027_research_claims.py`,
  `tests/test_research.py`, `.agent/plans/CLIP-046.md`.
- Edit: `clipos_api/main.py`, `tests/test_migrations.py`,
  `docs/development.md`.

## Security, rights, budget and external-side-effect impact
- No external side effects; org-scoped server-side queries; no secrets.
- Audits: `research_packets.registered`, `claims.registered`,
  `claims.veracity_changed`, `claim_sources.linked`.
- Claims/sources are planning inputs — no rights gate or budget impact.

## Implementation steps
1. Contracts: `ResearchPacket`, `Claim`, `ClaimSourceRequest`,
   `ClaimSource`, `VeracityRequest`; enums `ClaimVeracity`, `SourceSupport`.
2. SQL rows for three tables with unique/index set above.
3. `ResearchStore`: register_packet / register_claim (packet existence),
   set_veracity (audited transition, same-value no-op), link_source
   (claim existence, idempotent), getters/lists.
4. Routes (8): packets register/get/list, claims add/list (404 packet),
   sources link/list, veracity set; `_map_error` 404/409/422.
5. Migration 0027 (revises 0026, CREATE-only).
6. Wire main.py; test_migrations += 3 tables; docs section.

## Test/gate plan
- `ruff format --check`, `ruff check`, `mypy` (CI + `--python-version 3.12`).
- Full pytest SQLite; full pytest CI-env (PG/Redis/MinIO/Temporal).
- `alembic upgrade head` PG (0026 → 0027, idempotent).
- Real TestClient probes (PEP 563): roundtrip packet→claim→source→veracity,
  404/409/422 mappings, path/body id mismatch.
- OpenAPI export; `secret_scan`, `architecture_guard`, `state_audit verify`.

## Rollback / recovery plan
CREATE-only migration; revert merge if needed. State advance guarded by
validators. Worktree isolated; canonical main moves only after gates.

## Completion evidence
Ticket, branch/worktree, files, migration 0027, gates + results, suite
count, commit SHAs, CI run id on final push.

## Remaining risks / follow-ups
- CLIP-047 (brief/content-plan engine) will consume packets/claims.
- `research_jobs` execution records still unbuilt — dispatch of actual
  research runs is future scope (likely with an M11+ workflow ticket).
- Next free migration: 0028.
