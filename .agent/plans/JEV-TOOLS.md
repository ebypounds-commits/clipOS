# ExecPlan — Jev System One decision tools (ad-hoc)

## Objective (operator-requested, not a numbered ticket)
Implement "Jev AI tools" inside ClipOS: a provider-neutral decision-tool
module wrapping TypeSafe's System One protocol (choice/noul/score typed
questions, no text generation), with a deterministic mock for CI and an
env-configured remote adapter for the operator's existing TypeSafe API
key. Operator decisions (2026-10-12): adapter module + tools; TypeSafe
key exists (env-only, never in repo); pause CLIP-064, land this first.

## Current repository facts
- Head at plan time: `4397acf` (CLIP-063 state advance). CI green.
- CLIP-064 paused mid-implementation (local staging only, lease held,
  worktree untouched). Worktree `/srv/clipos/worktrees/jev-tools`,
  branch `agent/jev-tools` off main.
- No migration needed (audit-only persistence).
- Provider-neutrality guard scans providers.py only; secret_scan greps
  tracked files for key patterns — key comes from `JEV_API_KEY` env.

## Scope / non-scope
Scope: `clipos_api/jev_tools.py`, `tests/test_jev_tools.py`, main.py
router wiring, docs/development.md section, .agent/plans/JEV-TOOLS.md.
Non-scope: 10_TICKETS.yaml changes (ad-hoc, not a ticket — state audit
and BUILD_STATE untouched), frontend UI, Kev/Modal deployment, key
provisioning (operator sets env on the service).

## Implementation
1. Contracts: QuestionType/JevQuestion/JevRequest/JevAnswer/JevResult/
   JevHealth (frozen pydantic, model_alias caller-supplied, default
   systemone-latest — no vendor id).
2. MockJevAdapter: sha1-derived normalized probabilities, deterministic.
3. RemoteJevAdapter: httpx POST {JEV_BASE_URL}/v1/systemone + GET
   /v1/models health, Bearer auth from env, 30s timeout
   (JEV_TIMEOUT_SECONDS), error -> ValueError -> route 502, tolerant
   response parser (top-level or answers-wrapped).
4. Routes (Depends-before-decorator): POST /jev/answer,
   POST /jev/triage (category choice + optional urgency noul),
   GET /jev/health. Audit jev_tools.answered (org/tool/count/mode —
   never state text or key).
5. main.py: jev_tools router import + extend.

## Test/gate plan
~17 tests: contracts, mock determinism/sums/requirements, env selection,
remote MockTransport protocol verification (URL/auth/body/parse),
health 200/401, routes roundtrip/422/502, audit privacy. Gates: ruff
format/check, mypy, pytest SQLite + CI-env, OpenAPI probe, secret_scan,
arch_guard, state_audit verify (no state change).

## Landing
Worktree commit `feat: Jev System One decision tools — ...`, merge
--no-ff into main as an ad-hoc feature (no state transition), push, CI
green, remove worktree, then resume CLIP-064.

## Remaining risks
- Remote response schema tolerance covers top-level and answers-wrapped
  shapes; verify against the live endpoint once JEV_BASE_URL/JEV_API_KEY
  are set on the service (operator step, key never enters the repo).