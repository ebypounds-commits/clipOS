# CI/CD, Review & Deployment Specification

## Branch/worktree strategy
- protected `main`;
- one branch/worktree per ticket or tightly coupled ticket set;
- no unrelated changes;
- rebase/merge according to repository policy, never rewrite shared history silently;
- parallel Codex workers only on dependency-independent tickets;
- integration agent reviews overlapping files/contracts before merge.

## Required CI lanes
1. formatting/lint;
2. Python type/static checks;
3. TypeScript typecheck;
4. unit tests;
5. database migration up/down/upgrade tests where safe;
6. integration tests with PostgreSQL/Redis/object storage/Temporal;
7. tenant/RBAC/security regression tests;
8. workflow replay/idempotency tests;
9. agent schema/eval smoke tests;
10. secret scan;
11. dependency/SBOM vulnerability scan;
12. container build;
13. API contract/OpenAPI checks;
14. frontend build;
15. E2E smoke tests;
16. optional provider integration tests behind explicit secrets.

Never make CI green by deleting tests, reducing assertions, suppressing real errors, or excluding failing modules without an ADR and evidence.

## Review model
- Builder agent implements.
- Reviewer agent gets task, diff, tests, architecture rules; it must not rely on builder self-assessment.
- Security reviewer is required for auth, RBAC, secrets, external publishing, finance, sandbox, upload/URL ingestion, and infra changes.
- Migration reviewer checks backwards compatibility and rollback/roll-forward plan.

## Auto-merge
May be enabled only after M22 and only for low/medium-risk changes when:
- all required CI green;
- reviewer approves;
- no migration marked destructive;
- no secret/scope/financial/publication-policy increase;
- change is within current approved architecture.

## Environments
- local/dev;
- staging;
- production.

Staging mirrors production topology but uses non-production credentials/accounts and reduced resources.

## Deployment
`main -> build immutable images -> scan -> deploy staging -> migrate -> smoke/e2e/private publication test -> approval policy -> production canary -> health window -> full rollout`.

## Rollback
- images immutable/tagged by commit SHA;
- migrations favor expand/contract;
- application rollback must tolerate forward-compatible schema;
- destructive migrations are delayed until old app versions are no longer needed;
- deployment tool provides one-command rollback for app images;
- restore from backup is disaster recovery, not ordinary deployment rollback.

## Release evidence
Each release records commit SHA, image digests, migrations, config version, agent/policy versions, CI runs, security scan summary, deployment times, rollback target, and operator approvals.

## Hermes + Codex build-control additions
- Hermes owns ticket scheduling, leases, worktree allocation and integration sequencing.
- Codex works only inside its assigned ticket worktree by default.
- A branch/worktree lease must be acquired before dispatch.
- Build-state advancement is an orchestration transaction after accepted completion evidence, not a worker-side side effect.
- Concurrency starts at 1 during M-1/M0. Increase only after collision-free evidence; default maximum during early build is 2 independent tickets.
- The same VPS may host Hermes and Codex, but build credentials, ClipOS runtime credentials and production deployment credentials remain separate.
