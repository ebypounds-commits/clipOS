# ExecPlan — CLIP-001

## Ticket and objective
Bootstrap the M0 repository foundation: web and API skeletons, package metadata, local development stack, tooling, CI test lane, typed provider/module contracts, and audit logging.

## Current repository facts
- Branch/worktree lease: `agent/CLIP-001` at `/srv/clipos/worktrees/CLIP-001`.
- `BUILD_STATE.json` points to CLIP-001 and M0.
- M-1 evidence is complete. No application source exists yet.

## Scope / non-scope
- In scope: exact packet deliverables, `.gitignore`, CI test job, docs, and completion report.
- Out of scope: product features, database migrations, real provider adapters, production deployment, global build-state edits.

## Dependencies and assumptions
- `CLIP-P006` is DONE.
- Python, Docker, and Git are available. Node/npm may be unavailable on the VPS; if so, TypeScript configuration is validated without npx and the network blocker is documented.

## Files/domains expected to change
`apps/web/**`, `services/api/**`, `docker-compose.dev.yml`, `Makefile`, `.github/workflows/baseline-ci.yml`, `docs/development.md`, `.agent/**`, `.gitignore`.

## Security, rights, budget and external-side-effect impact
- No production secrets or credentials; compose values are clearly local-only.
- No external publishing or media actions.
- Structured audit logging establishes an append-only local record for future material state changes.
- Package installs are the only permitted network operations.

## Implementation steps
1. Add FastAPI health, provider protocols, alias registry, bounded-module enum, and audit logger.
2. Add focused tests for health, registry/no-hardcoded-provider identifiers, audit, and modules.
3. Add minimal typed Next.js skeleton.
4. Add dev stack, developer commands, docs, and CI test lane.
5. Run and repair all required gates.
6. Commit on the leased branch and write the completion report.

## Test/gate plan
- `python3 -m pytest -q services/api`
- `ruff check services/api`
- `docker compose -f docker-compose.dev.yml config -q`
- `python3 scripts/validate_handoff.py`
- `bash scripts/secret_scan.sh`
- TypeScript typecheck when available.

## Completion evidence
Persisted at `.agent/dryrun/CLIP-001_completion_report.json` after gates and commit.

## Remaining risks / follow-ups
CI runtime/package versions and actual local container startup are later-ticket operational qualifications.
