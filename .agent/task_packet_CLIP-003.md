# Codex Task Packet — CLIP-003

## Lease
- ticket_id: CLIP-003
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-003
- worktree: /srv/clipos/worktrees/CLIP-003
- lease_expires_at: +60 minutes from dispatch

## Objective
Establish CI quality gates and dependency/security scans per 11_CI_CD_DEPLOYMENT.md ("Required CI lanes"), implementing the lanes that apply to the current codebase and documenting deferred lanes with reasons. CI must enforce (fail) — never silence findings.

## Environment constraints
- This sandbox has NO network and NO docker socket: pip/npm installs and container runs are impossible IN-SESSION (report SKIP+reason honestly; orchestrator runs them outside the sandbox).
- OCCUPIED host ports (never bind): 22 53 80 443 3000 3080 3100 5432 6001 6002 8000 8080 8090 8642 8776 8888 9119 9377 11235 18080. Compose already uses 5433/6380/9002/9003 (keep). For ad-hoc container runs in scripts use 18099.

## Required reading
11_CI_CD_DEPLOYMENT.md (the16 lanes + "never make CI green by deleting tests/suppressing errors"), 07_SECURITY_GOVERNANCE_RIGHTS.md, 12_OPERATIONS_RUNBOOK.md, 09_MILESTONES_M0_M24.md, AGENTS.md, docs/development.md, .github/workflows/baseline-ci.yml (current lanes).

## Lane decisions (orchestrator, per spec) — implement ALL of these
1. **formatting/lint**: add `ruff format --check services/api` alongside `ruff check` (run ruff format once to normalize existing code so the check passes).
2. **Python static/type**: add `mypy services/api/clipos_api` with a pragmatic config in pyproject (`python_version=3.12`, `check_untyped_defs=true`, NOT strict; add annotations where mypy reports real issues; document any `# type: ignore[rule]` in docs/development.md).
3. **TypeScript typecheck**: keep existing tsc step.
4. **unit tests + coverage**: add pytest-cov; measure coverage of `clipos_api`; set `--cov-fail-under` in pyproject to (measured − 5, min 60) so regressions fail; keep all14 existing tests green (NEVER delete/weaken tests).
5. **integration (infra)**: new `scripts/integration_smoke.sh` — against the running dev stack: real `psql` SELECT 1 via docker exec, real `redis-cli PING` via docker exec, MinIO ready check; JSON summary + exit 0/1, read-only (idempotent). Wire it into CI test job AFTER the health script while the stack is up.
6. **secret scan**: keep.
7. **dependency/SBOM**: add to [dev] extras: `pip-audit`, `cyclonedx-py`. New CI step `quality` job: `pip-audit -r <pinned runtime+dev deps listing>` (generate `requirements-audit.txt` from the resolved environment or `-e .[dev]` supported mode) — FAIL on findings; unfixable/no-fix CVEs may be listed in `scripts/audit-ignores.txt` as `<CVE> # justification` (one per line, checked in, each with a written reason) and passed via repeated `--ignore-vuln`. `npm audit --audit-level=high` in apps/web. SBOM: `cyclonedx-py` emits `sbom/api-python.cdx.json` (artifact path; gitignore it).
8. **container build**: create `services/api/Dockerfile` (python:3.12-slim, COPY pyproject/src, pip install -e ., non-root USER, CMD uvicorn clipos_api.main:app --host 0.0.0.0 --port 8000) and `apps/web/Dockerfile` (node:22-alpine, npm ci, next build, standalone output ok). CI build job: `docker compose -f docker-compose.dev.yml build` for the `app` profile services (api+web).
9. **API contract**: script or step exporting FastAPI OpenAPI: `PYTHONPATH=services/api python3 -c "import json; from clipos_api.main import app; json.dump(app.openapi(), open('openapi.json','w'), indent=2)"` + assert `/health` path present + valid JSON; run in CI (quality job) and as `make openapi`.
10. **frontend build**: CI build job runs `npm ci && npm run build` in apps/web (add `"build": "next build"` script to package.json).
11. **e2e smoke**: new `scripts/e2e_smoke.sh` — build/run the api image on port 18099, poll `curl -fsS localhost:18099/health` until 200 (≤30s), assert JSON has status field, then ALWAYS cleanup (docker rm -f, trap). Wire into CI build job after image build.
12. **report job**: `needs: [validate, test, quality, build]` and success condition requires ALL FOUR == success.
13. **docs**: docs/development.md gains a "CI lane matrix" table: all16 spec lanes × status (Implemented (which job/step) | Deferred — reason tied to not-yet-existing feature, e.g. migrations→no Alembic yet, tenant/RBAC→no auth code yet, workflow replay→no engine yet, agent eval→registries later, provider tests→behind future secrets, Temporal→not in dev stack yet).
14. **Makefile**: add `quality` target running format-check, lint, mypy, pytest-with-cov, pip-audit, npm audit, openapi check (operator-visible single entry point); keep existing targets working.
15. `.agent/dryrun/CLIP-003_completion_report.json` per schema.

## Acceptance (orchestrator re-runs ALL outside sandbox)
- ruff format --check + ruff check + mypy clean; pytest with coverage ≥ floor; 14+ tests green.
- pip-audit runs and reports (findings either fixed or in scripts/audit-ignores.txt with justification); npm audit passes at high level.
- openapi export succeeds with /health present.
- services/api/Dockerfile + apps/web/Dockerfile build (`docker compose build` app profile); scripts/e2e_smoke.sh exits 0 live.
- scripts/integration_smoke.sh exits 0 against live stack.
- workflow YAML valid; jobs validate/test/quality/build present; report needs all four.
- docs/development.md lane matrix present; validate_handoff OK; secret_scan clean; 10_TICKETS/BUILD_STATE/docs/reports untouched.

## Allowed scope
paths: .github/workflows/baseline-ci.yml, scripts/**, services/api/** (incl. Dockerfile, pyproject), apps/web/** (incl. Dockerfile, package.json), Makefile, docs/development.md, .gitignore, .agent/**
network: none in-session (no pip/npm/push)
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; deleting or weakening tests/assertions; suppressing findings without a documented ignore entry; binding occupied ports; fabricating results (report SKIP+reason).

## Required gates (runnable here)
- python3 -m compileall services/api; python3 -m pytest -q services/api (if pytest importable else SKIP)
- docker compose -f docker-compose.dev.yml config -q (if docker responds else SKIP)
- python3 scripts/validate_handoff.py; bash scripts/secret_scan.sh
- yaml parse of .github/workflows/baseline-ci.yml

## Completion output
Completion report (BLOCKED only for genuinely sandbox-blocked gates with reasons), concise summary, no fabrication.
