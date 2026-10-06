# ADR 0002: Core stack selection

## Status

Accepted

## Date

2026-10-06

## Context

The foundation needs a fixed core stack so that subsequent milestones build on one coherent set of components. `02_SYSTEM_ARCHITECTURE.md` §2 enumerates the stack; each selection below is quoted from that section.

## Decision

| Stack item | Decision | Spec source |
|---|---|---|
| Web | Next.js, React, TypeScript | 02§2 |
| API/application | FastAPI, Python, Pydantic, SQLAlchemy, Alembic | 02§2 |
| Database | PostgreSQL with pgvector extension | 02§2 |
| Cache/rate-limits | Redis | 02§2 |
| Durable orchestration | Self-hosted Temporal | 02§2 |
| Object storage | S3-compatible (MinIO locally; production can use MinIO, Cloudflare R2, S3, or compatible service) | 02§2 |
| Media | FFmpeg/ffprobe; optional GPU acceleration exposed as capability, never assumed | 02§2 |
| Agent runtime | Python; OpenAI Agents SDK may be used for OpenAI-backed specialists, but registry/runtime abstractions must not make the application provider-locked | 02§2 |
| LLM gateway | Internal interface with optional LiteLLM deployment; provider adapters behind logical model aliases | 02§2 |
| Events | Transactional outbox initially; NATS JetStream only when event throughput/decoupling justifies it | 02§2 |
| Observability | OpenTelemetry, Prometheus, Grafana, Loki; optional Sentry | 02§2 |
| Reverse proxy/TLS | Caddy or Traefik | 02§2 |
| Packaging | Docker Compose first; k3s/Kubernetes only after measured need | 02§2 |

Consistency rules that follow from these choices (02§9): database mutations and emitted domain events use the transactional outbox; external writes use idempotency records; workflow state is durable in Temporal; analytics ingestion is eventually consistent; the finance ledger is append-only and reconciled.

## Alternatives considered

Alternatives are not enumerated per-item in the specs; the governing constraints are stated instead. PostgreSQL is required because pgvector metadata lives in the same transactional store as business state (02§2, 02§6). Redis is the specified cache/rate-limit layer. Temporal is the specified durable workflow engine. NATS is explicitly deferred until throughput/decoupling justifies it, and Kubernetes until Docker Compose operational limits are proven (02§2, 02§5). Deviating from any row requires a new ADR.

## Consequences

- Subsequent milestones implement against these components; introducing a competing component for the same role requires an ADR.
- Data-model conventions from 02§6 apply: PostgreSQL stores metadata/state, not large media blobs; object storage holds media and evidence artifacts; secrets are referenced by `secret_ref`, never copied into application tables.
- Every DB schema change uses an Alembic migration with tests (AGENTS.md engineering rules).

## Compliance impact

None directly beyond what the spec already mandates for the chosen components: dependency/secret/container-image scanning in CI (07), non-production local credentials for dev infrastructure, and no Docker socket exposure to application/agent containers (07, AGENTS.md).

## Related

- `02_SYSTEM_ARCHITECTURE.md` §2, §5, §6, §9
- ADR 0001 (deployment topology), ADR 0003 (provider aliases), ADR 0004 (CI gates)
