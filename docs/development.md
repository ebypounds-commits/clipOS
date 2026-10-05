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
make devstack
```

This starts PostgreSQL with pgvector, Redis, and MinIO using clearly non-production local credentials. Optional API and web containers are grouped behind the Compose `app` profile and are not started by the M0 skeleton.

## M0 evidence map

- Repository/package skeleton: `apps/web/`, `services/api/`.
- Provider abstraction and logical alias contract: `services/api/clipos_api/providers.py`, `registry.py`.
- Bounded modules: `services/api/clipos_api/modules.py`.
- Operator-visible audit contract: `services/api/clipos_api/audit.py`.
- Development topology: `docker-compose.dev.yml`.
- Required gates: packet commands; results are recorded in `.agent/dryrun/CLIP-001_completion_report.json`.
