# CLIP-094 — Hardened production Compose/staging topology (M23)

## Goal

Ship the staging and production Compose topologies, hardened to a fixed set
of invariants that CI enforces, so a drift in either file fails the build
rather than the deployment.

## Deliverables

| Path | Purpose |
| --- | --- |
| `deploy/compose.production.yml` | Hardened production topology |
| `deploy/compose.staging.yml` | Staging; mirrors production service-for-service |
| `deploy/Caddyfile` | Production TLS edge (auto-cert, security headers) |
| `deploy/Caddyfile.staging` | Staging edge (plain HTTP, same header/upstream policy) |
| `deploy/env.production.example` | Production variable names only |
| `deploy/env.staging.example` | Staging variable names only |
| `deploy/README.md` | Topology map, invariants table, bring-up commands |
| `services/api/clipos_api/deployment_topology.py` | Typed contracts + pure invariant validators |
| `scripts/deploy_topology_check.py` | CLI harness emitting a PASS/FAIL JSON report |
| `services/api/tests/test_deployment_topology.py` | 25 tests |

## Hardening invariants

| ID | Invariant |
| --- | --- |
| H1 | Internal services (postgres, redis, minio, temporal) publish no host port |
| H2 | No development credential marker (`clipos_dev`, `*_local_only`) |
| H3 | Every long-running service declares a non-`"no"` restart policy |
| H4 | Every long-running service declares a healthcheck |
| H5 | No privileged service and no `docker.sock` mount |
| H6 | Every long-running service declares cpu + memory limits |
| H7 | No secret-like literal in the file |
| H8 | Reference-only keys (`DATABASE_URL`, `STORAGE_SECRET_KEY`, …) use `${VAR}` |

One-shot init containers (`clipos.role: one-shot-init`) are exempt from
H3, H4 and H6: they run once, exit, and hold no resources between runs.

## Design notes

- **Two bridges.** `clipos_edge` faces the host; `clipos_internal` is
  declared `internal: true`, so Postgres/Redis/MinIO/Temporal have no
  route off the host. Only the `edge` service publishes ports (80/443 in
  production, 8080 in staging).
- **Immutable images.** `CLIPOS_API_IMAGE` / `CLIPOS_WEB_IMAGE` are
  digest-pinned, tagged by commit SHA; app rollback is a re-pin of those
  two variables (see `11_CI_CD_DEPLOYMENT.md`).
- **Secrets by reference.** No credential is literalled. H7 scans for
  secret shapes; H8 checks that reference-only keys are `${VAR}`.
- **Pure validation.** `deployment_topology.py` never touches Docker, so
  validation is fast and runs in CI. `deploy_topology_check.py` adds a
  ninth check: staging and production must define the same service set.
- **`pyyaml` declared.** `pyyaml>=6.0` added to `services/api` deps
  because the CI `test` job installs only `services/api[dev]`; the
  `validate` job installed PyYAML ad hoc but the test job did not.

## Verification

- `deploy_topology_check.py` → VERDICT PASS, both topologies 8/8, mirror PASS.
- 25 new tests (pure invariants incl. regression cases, CLI harness,
  real repo topologies).
- ruff format + check clean; mypy clean (baseline 2 pre-existing errors).
- SQLite suite 1584 passed / 12 skipped / 93.56% cov.
- CI-env (Postgres + Redis + MinIO + Temporal) 1595 passed / 1 skipped /
  93.84% cov.
- `alembic upgrade head` clean (no migration: topology/docs only).
- secret_scan clean, architecture_guard all six checks OK, state_audit 99/99.
