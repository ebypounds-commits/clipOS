# Codex Task Packet — CLIP-001

## Lease
- ticket_id: CLIP-001
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-001
- worktree: /srv/clipos/worktrees/CLIP-001
- lease_expires_at: +60 minutes from dispatch

## Objective
Bootstrap the M0 engineering foundation: monorepo layout, package management, Docker dev stack, developer toolchain, CI test lane, typed contract/provider stubs, and operator-visible audit logging — exactly per the repository specifications. Scaffolding and contracts only; no product features.

## Required reading (in the worktree, read before writing)
00_README_FIRST.md, 02_SYSTEM_ARCHITECTURE.md (§1 style, §2 stack, §4 modules, §7 provider abstraction), 03_DOMAIN_DATA_MODEL.md, 09_MILESTONES_M0_M24.md (M0 exit gates), 11_CI_CD_DEPLOYMENT.md, AGENTS.md, MASTER_CODEX_PROMPT.md. Follow build doctrine: deterministic-first, provider-locked identifiers forbidden, capability-gated external actions.

## Deliverables (exact paths — orchestrator verifies these)
1. `apps/web/` — Next.js/React/TypeScript skeleton: package.json (name @clipos/web, private), tsconfig.json, next.config.js, minimal app dir with one page. NO full product UI.
2. `services/api/` — FastAPI skeleton: pyproject.toml (project clipos-api; deps: fastapi, pydantic, sqlalchemy; optional-deps dev: pytest, httpx, ruff), package `clipos_api/` with: `main.py` (FastAPI app + GET /health returning {status, version}), `providers.py` (Protocol/ABC interfaces for the 11 providers from 02§7 — LLMProvider, EmbeddingProvider, TranscriptionProvider, TextToSpeechProvider, ImageGenerationProvider, VideoGenerationProvider, SearchProvider, PublishingProvider, AnalyticsProvider, AffiliateProvider, StorageProvider — each with method signatures raising NotImplementedError), `registry.py` (provider registry keyed by logical alias, MUST reject unknown aliases and MUST NOT contain any literal provider/model identifier), `audit.py` (append-only structured audit log: `record(action, actor, subject, details)` writing JSON lines to .agent/audit/audit.log with timestamp; material state changes go through it), and `modules.py` defining the 21 bounded-module names from 02§4 as an enum + import-isolation test helper.
3. `services/api/tests/test_health.py` (health endpoint via fastapi TestClient), `tests/test_registry.py` (alias rejection + no-hardcoded-identifier scan over clipos_api/ source: fail if regex `gpt-|claude-|glm-|deepseek|gemini|llama-\d|mistral` matches outside providers.py comments), `tests/test_audit.py` (records parse as JSON with required fields), `tests/test_modules.py` (21 module names present).
4. `apps/web` typecheck: `npx tsc --noEmit` must pass (keep deps minimal; if network install fails, record blocker and keep tsconfig valid).
5. `docker-compose.dev.yml` — services: postgres (postgres:16 with pgvector: use `pgvector/pgvector:pgvector:pg16` image, healthcheck), redis:7-alpine, minio (minio/minio, healthcheck), plus commented/optional api+web profiles. Must pass `docker compose -f docker-compose.dev.yml config` (no secrets, no real credentials; use env defaults like dev-only passwords clearly marked).
6. `Makefile` — targets: `setup` (pip install -e ./services/api[dev] into .venv or uv equivalent), `test` (pytest in services/api), `lint` (ruff check services/api), `typecheck` (tsc in apps/web), `devstack` (docker compose -f docker-compose.dev.yml up -d), `validate` (validate_handoff + secret_scan + tests).
7. CI: extend `.github/workflows/baseline-ci.yml` — add job `test` (needs nothing, runs alongside validate): checkout, setup-python 3.12, `pip install -e "services/api[dev]"`, `ruff check services/api`, `pytest -q services/api`, `docker compose -f docker-compose.dev.yml config -q`. Keep existing validate job and report job UNCHANGED except `needs:`/`if:` adjustments required to keep report accurate (report must still reflect overall status).
8. `docs/development.md` — how to run setup/test/lint/devstack locally on the VPS; maps M0 exit gates to evidence.
9. `.agent/dryrun/CLIP-001_completion_report.json` — per schemas/codex_completion_report.schema.json.

## Acceptance (orchestrator re-runs all of these)
- pytest green (all 4 test files), ruff clean, `docker compose -f docker-compose.dev.yml config -q` exit 0, `npx tsc --noEmit` in apps/web exit 0 (or documented network blocker + valid tsconfig).
- No literal provider/model identifiers outside providers.py comments (grep gate above passes as a test).
- CI workflow valid YAML, still contains validate+report jobs, adds test job with the pytest/ruff/compose steps.
- validate_handoff.py OK; secret_scan.sh clean; git status clean after commit on branch agent/CLIP-001.
- 10_TICKETS.yaml, BUILD_STATE.json, docs/reports/* UNTOUCHED (orchestrator handles state).

## Allowed scope
paths: apps/web/**, services/api/**, docker-compose.dev.yml, Makefile, .github/workflows/baseline-ci.yml, docs/development.md, .agent/**, .gitignore updates for .venv/node_modules/.agent/audit/
migrations allowed: no
external network actions allowed: only package installs (pip/npm) needed to run the gates; no pushes, no gh api, no deploy keys.

## Forbidden actions
- do not push or merge to main; do not touch main, 10_TICKETS.yaml, BUILD_STATE.json
- do not select unrelated tickets; do not weaken baseline-ci validate/report jobs
- do not expose secrets; do not modify other worktrees

## Required gates (run in the worktree before finishing)
- python3 -m pytest -q services/api (all pass)
- ruff check services/api
- docker compose -f docker-compose.dev.yml config -q
- python3 scripts/validate_handoff.py
- bash scripts/secret_scan.sh

## Completion output
Run the gates, print their results, commit on this branch if git allows (if the gitdir is read-only, write the tree state and a completion report — orchestrator lands commits), then print a concise summary including any blockers.
