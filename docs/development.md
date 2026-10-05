# ClipOS local development

The M0 foundation intentionally contains no product features. It establishes package boundaries, typed contracts, local infrastructure definitions, and repeatable quality commands.

## Environment setup

```bash
make setup
```

This creates `.venv` and installs the API in editable mode with development dependencies. Web type checking uses package installation on demand; if Node is unavailable, record the execution environment blocker and continue with server-side gates.

## Tests, linting, and type checks

```bash
make test
make lint
make typecheck
```

The Python suite covers the health contract, provider alias rejection, absence of hard-coded vendor/model identifiers, audit records, and the 21 bounded modules.

## Local infrastructure

```bash
make infra-up
```

This starts PostgreSQL with pgvector, Redis, and MinIO using clearly non-production local credentials, waits for their Compose health checks, and runs the infrastructure health gate. Use `make infra-down` to stop the stack and `make infra-health` to re-check it without changing container state; the script is read-only and repeatable. Optional API and web containers are grouped behind the Compose `app` profile and are not started by the M0 skeleton.

The Compose project name is fixed to `clipos-dev`, so repeated commands address the same stack regardless of the shell directory. Host ports are intentionally non-colliding: PostgreSQL uses 5433, Redis 6380, and MinIO 9002/9003. Host port 5432 is reserved by the operator's live PostgreSQL service and must never be bound by development infrastructure.

## M0 evidence map

- Repository/package skeleton: `apps/web/`, `services/api/`.
- Provider abstraction and logical alias contract: `services/api/clipos_api/providers.py`, `registry.py`.
- Bounded modules: `services/api/clipos_api/modules.py`.
- Operator-visible audit contract: `services/api/clipos_api/audit.py`.
- Development topology: `docker-compose.dev.yml`.
- Required gates: packet commands; results are recorded in `.agent/dryrun/CLIP-001_completion_report.json`.
