# ADR 0001: Modular monolith and worker processes

## Status

Accepted

## Date

2026-10-06

## Context

ClipOS must move from empty foundation to product features without betting the build on a distributed system it does not yet operate. `02_SYSTEM_ARCHITECTURE.md` §1 sets the architectural style: start as a **modular monolith plus dedicated worker processes**; domain boundaries are explicit, but premature microservices are avoided. Media rendering, durable workflow workers, and optionally local inference run as separate processes/containers because they have different resource profiles.

## Decision

ClipOS is built as a modular monolith with explicit bounded modules (the 21 modules enumerated in `02_SYSTEM_ARCHITECTURE.md` §4) plus dedicated worker processes/containers. Cross-module changes go through application services and typed contracts; modules never import another module's persistence internals directly. Work with distinct resource profiles — media rendering, durable workflow workers, and optionally local inference — is deployed as separate processes/containers per the §5 topology (worker-core, worker-agent, worker-media, worker-publisher).

## Alternatives considered

- **Microservices from day one.** Rejected: the spec explicitly avoids premature microservices; the initial single-server topology (02§5) has no operational need for them yet.
- **Single undifferentiated process.** Rejected: media rendering, durable workflows, and local inference have different resource profiles, so they must be separable and independently scalable (02§1, 02§5 scale-out path).

## Consequences

- New domain code lives inside its bounded module and crosses boundaries only via typed contracts.
- The §5 container topology is the deployment contract; scale-out (separate DB host, more media workers, GPU pool, external object storage, NATS, k3s) happens only on measured need.
- Moving logic out of the monolith later is a deliberate architecture change requiring a new ADR.

## Compliance impact

Module boundaries are the enforcement surface for tenant scoping, RBAC/capability checks at service boundaries, and audit of privileged changes (07§Mandatory controls). The monolith structure does not relax any security control; it concentrates them at service boundaries.

## Related

- `02_SYSTEM_ARCHITECTURE.md` §1, §3–§5, §10
- `11_CI_CD_DEPLOYMENT.md` (review model)
- ADR 0002 (stack selection), ADR 0004 (CI gates)
