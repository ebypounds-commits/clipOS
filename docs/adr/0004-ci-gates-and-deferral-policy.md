# ADR 0004: CI gates and deferral policy

## Status

Accepted

## Date

2026-10-06

## Context

`11_CI_CD_DEPLOYMENT.md` defines 16 required CI lanes and the rule "Never make CI green by deleting tests, reducing assertions, suppressing real errors, or excluding failing modules without an ADR and evidence." The M0 foundation (through CLIP-003) implements a subset of the lanes; the rest are deferred because the corresponding capabilities do not exist yet. This ADR records which lanes are implemented, which are deferred and why, so that deferral is a governed decision instead of silent suppression.

## Decision

The required lanes of 11§Required CI lanes map to the current implementation as recorded in `docs/development.md` (CLIP-003 lane matrix):

| # | Lane | Status |
|---|------|--------|
| 1 | Formatting/lint | Implemented — `test` job: `ruff format --check` + `ruff check` |
| 2 | Python type/static checks | Implemented — `test` job: mypy |
| 3 | TypeScript typecheck | Implemented — `test` job: `npx tsc --noEmit` |
| 4 | Unit tests (+coverage) | Implemented — `test` job: pytest with coverage floor |
| 5 | Database migration up/down tests | Deferred — no Alembic migrations or schema yet (M2) |
| 6 | Integration tests (PostgreSQL/Redis/object storage/Temporal) | Partially implemented — PostgreSQL/Redis/MinIO smoke runs; Temporal deferred (not in dev stack yet, M3) |
| 7 | Tenant/RBAC/security regression tests | Deferred — no auth, tenant, or RBAC code yet (M1) |
| 8 | Workflow replay/idempotency tests | Deferred — no workflow engine yet (M3) |
| 9 | Agent schema/eval smoke tests | Deferred — agent/model registries arrive in M4/M5; registry unit tests exist, no eval harness yet |
| 10 | Secret scan | Implemented — `validate` job |
| 11 | Dependency/SBOM vulnerability scan | Implemented — `quality` job: pip-audit, SBOM, npm audit |
| 12 | Container build | Implemented — `build` job |
| 13 | API contract/OpenAPI checks | Implemented — `quality` job |
| 14 | Frontend build | Implemented — `build` job |
| 15 | E2E smoke tests | Implemented — `build` job |
| 16 | Provider integration tests behind secrets | Deferred — provider credentials/human gates not configured yet (M5+); must activate behind explicit secrets |

Rules:

1. The four workflow jobs `validate`, `test`, `quality`, and `build` are all required; the `report` job records success only when all four succeed.
2. A lane may be deferred only with the recorded reason and target milestone above; activating a deferred lane is the default once its capability exists.
3. Suppression — deleting tests, reducing assertions, suppressing real errors, or excluding failing modules — is forbidden without an ADR and evidence (11§Required CI lanes).
4. Governance wiring introduced by CLIP-004 runs in the `validate` job: `python3 scripts/validate_state.py && python3 scripts/architecture_guard.py` after the secret scan.

## Alternatives considered

- **Weakening or deleting failing checks to go green.** Explicitly forbidden by 11 and AGENTS.md ("never disable, weaken, or delete meaningful tests merely to pass CI"; "never green by suppression").
- **Deferring without a record.** Rejected: deferral without reason/milestone is indistinguishable from suppression; this ADR and `docs/development.md` are the record.

## Consequences

- Every future ticket must keep the implemented lanes green and must activate a deferred lane as its dependency lands (e.g. migration tests with the first Alembic migration in M2).
- The architecture guard becomes a CI gate; architecture violations fail `validate`.
- This ADR must be superseded/updated when the deferral map materially changes.

## Compliance impact

Enforces the governance posture of 11 (never green by suppression, full-job reporting) and adds machine-checkable state/architecture validation. Security-relevant lanes (secret scan, dependency/SBOM scans, security regression tests once applicable) remain mandatory and are never suppressible without an ADR and evidence.

## Related

- `11_CI_CD_DEPLOYMENT.md`
- `docs/development.md` (CI lane matrix)
- CLIP-003 evidence, `BUILD_STATE.json`
- ADR 0003 (guard rules)
