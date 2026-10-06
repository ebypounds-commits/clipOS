# ADR 0003: Provider registry alias policy

## Status

Accepted

## Date

2026-10-06

## Context

ClipOS is model-agnostic by doctrine. `00_README_FIRST.md` build doctrine 6 states: "Operational LLMs are specialists behind Agent/Model/Tool registries; no production agent hard-codes a provider/model." `02_SYSTEM_ARCHITECTURE.md` §7 defines the provider abstraction: core domains use provider interfaces (LLMProvider, EmbeddingProvider, TranscriptionProvider, TextToSpeechProvider, ImageGenerationProvider, VideoGenerationProvider, SearchProvider, PublishingProvider, AnalyticsProvider, AffiliateProvider, StorageProvider), and provider-specific DTOs remain inside adapters. The M0 foundation implemented this in `services/api/clipos_api/providers.py` (interfaces only; its comments use generic compatibility descriptors, not configuration identifiers) and `services/api/clipos_api/registry.py` (logical alias registry).

## Decision

- Production code never references a concrete provider or model identity; it resolves providers only through **stable logical aliases** owned by configuration.
- `services/api/clipos_api/registry.py` is the enforcement point: `register`/`get`/`clear` are the only registration path, aliases are validated, duplicates are refused, and unknown aliases fail closed.
- Provider-specific transport and DTO details stay inside adapters behind the 02§7 interfaces.
- Agent/runtime abstractions must not make the application provider-locked (02§2 agent runtime row).

## Alternatives considered

- **Hard-coded provider/model names in domain or agent code.** Rejected: violates doctrine 6 and couples core domains to vendors; also makes provider swaps and capability routing impossible without core changes.
- **Direct SDK calls from core domains.** Rejected: 02§7 requires interfaces at the core boundary with adapters owning provider specifics.

## Consequences

- `scripts/architecture_guard.py` statically checks that `providers.py` contains no literal provider/model identifiers and that `registry.py` still exposes `register`, `get`, and `clear`; a violation fails CI.
- Adding a provider means adding an adapter plus configuration mapping aliases to concrete models — no core change.
- Registry tests must continue to cover alias rejection and unknown-alias failure as the enforcement contract.

## Compliance impact

Supports the model-agnostic, capability-gated posture of 00 doctrine 6/7 and 07: external actions remain auditable and budgeted regardless of provider, and provider identity is configuration (never a secret in code). No provider/model identifier is hard-coded where a registry alias is required (CLIP-004 acceptance).

## Related

- `00_README_FIRST.md` doctrine 6
- `02_SYSTEM_ARCHITECTURE.md` §2 (agent runtime, LLM gateway), §7
- `05_MODEL_TOOL_PLUGIN_REGISTRY.md`
- ADR 0004 (guard enforcement in CI)
