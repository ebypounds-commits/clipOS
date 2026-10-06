# Architecture Decision Records

This directory holds ClipOS architecture decision records (ADRs). An ADR records a material, lasting engineering decision together with its context, alternatives, and compliance impact so that later work can be checked against the decision instead of rediscovering it.

## When a decision needs an ADR

Write an ADR when a change is hard to reverse or constrains future tickets, including:

- architectural style, module boundaries, or service/process topology;
- selection of a core framework, datastore, or infrastructure component;
- provider/model abstraction policy or anything that could couple the application to a specific provider or model;
- CI/CD, release, or governance policy, including deferring a required gate;
- security posture: auth, RBAC, secrets handling, external publishing, finance controls, sandboxing, upload/URL ingestion, or infrastructure changes.

Routine implementation choices that follow an existing ADR or spec do not need a new ADR. If implementation reality would deviate materially from the architecture, write the ADR before changing the architecture (AGENTS.md, "Source of truth").

## Numbering

- Files are numbered `NNNN-short-title.md` starting at `0001`; `0000-template.md` is reserved for the template.
- Numbers are never reused. A superseded record keeps its number and points to its successor.

## Status lifecycle

- `Proposed` — drafted, under review, not yet binding.
- `Accepted` — binding; implementation and CI rules may enforce it.
- `Superseded` — replaced by a later ADR; the record stays for history and links to the successor.
- `Deferred` — deliberately not decided now; the record states what evidence would reopen it.

## Review requirements

Per `11_CI_CD_DEPLOYMENT.md`, the reviewer agent reviews the diff, tests, and architecture rules and must not rely on builder self-assessment. A **security reviewer is required** for ADRs touching auth, RBAC, secrets, external publishing, finance, sandboxing, upload/URL ingestion, or infrastructure. Migration-affecting decisions additionally require review of backwards compatibility and the rollback/roll-forward plan. CI suppression (deleting tests, reducing assertions, suppressing real errors, or excluding failing modules) is forbidden without an ADR and evidence.

## Records

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-modular-monolith-and-worker-processes.md) | Modular monolith and worker processes | Accepted |
| [0002](0002-core-stack-selection.md) | Core stack selection | Accepted |
| [0003](0003-provider-registry-alias-policy.md) | Provider registry alias policy | Accepted |
| [0004](0004-ci-gates-and-deferral-policy.md) | CI gates and deferral policy | Accepted |
