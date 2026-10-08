# Model, Tool, Plugin & Connector Registry

## 1. Model Registry
Agents reference logical aliases, never provider model IDs directly.

Example aliases:
- `reasoning-premium`
- `reasoning-standard`
- `research-fast`
- `vision-premium`
- `vision-standard`
- `caption-lowcost`
- `classification-local`
- `embedding-default`
- `transcription-default`
- `tts-default`
- `coding-agent` (used by the engineering loop, not ordinary content runs)

Each alias has ordered targets with:
- provider;
- actual model/deployment identifier;
- region/endpoint;
- capability flags;
- price metadata;
- max context/output;
- enabled state;
- health score;
- retry/fallback rules;
- data handling classification.

## 2. LLM Gateway
Implement a provider-neutral `AIClient` interface and a routing service. LiteLLM may be deployed as a gateway, but ClipOS must retain its own registry/policy layer so the product can replace the gateway without rewriting domains.

Routing inputs:
- required capabilities;
- agent policy;
- current provider health;
- spend budget;
- expected token volume;
- latency class;
- data sensitivity;
- experiment assignment.

Routing output must record actual provider/model selected for audit and cost attribution.

Implemented in `services/api/clipos_api/llm_gateway.py` (CLIP-023): the `LLMGateway` resolves logical aliases through `ModelRegistryStore.resolve_alias` and dispatches to provider adapters in priority order. Provider adapters are registered by provider name at startup through pluggable factories (`register_adapter`); provider SDKs are never imported at module scope. Failed routes are marked `degraded` (audited) and the next route is tried. Dispatch is audited start-to-finish and idempotent per `execution_id` via an in-memory response cache. Production adapter factories are operator wiring; no real provider adapters ship in core.
## 3. Tool Registry
Tool definition fields:
- stable `tool_id`;
- semantic version;
- description;
- JSON input/output schema;
- capabilities exposed;
- risk class;
- required secret refs;
- allowed network destinations;
- timeout;
- retries;
- idempotency support;
- cost model;
- data classification;
- health check;
- enabled state.

Implemented in `services/api/clipos_api/tool_registry.py` (CLIP-026): tools are persisted by their stable `tool_id` with capability scopes, per-run budget, enabled state, and metadata. Re-registration is idempotent only when immutable definition fields match. Capability grants attach a tool to an immutable agent-version identity, matching the agent registry's `tool_grants` and `prohibited_tools` contract. Grant and revoke transitions are append-only audited, capability checks require an enabled tool, a registered agent version, an active grant, and no version prohibition, and revalidation drops active grants when a tool is disabled.

## 4. Plugin types
### Native adapter
For critical/high-throughput integrations: YouTube, TikTok, Meta, storage, transcription, major model providers.

### MCP server
For controlled, discoverable tool surfaces where MCP is appropriate. Prefer private/self-hosted MCP for internal systems and hosted MCP only when trust/network policy permits.

Implemented in `services/api/clipos_api/mcp_boundary.py` (CLIP-028): the boundary speaks a minimal JSON-RPC 2.0 `tools/call` POST subset over HTTP and does not import an MCP SDK. The plugin must be registered as `mcp_server` and enabled; private/self-hosted metadata is trusted by default, while a plugin marked `hosted` is callable only with an explicit `trusted: true` metadata flag. Each call is additionally gated by the Tool Registry's agent-version capability check. The `config_ref` names the endpoint environment variable and is resolved by the endpoint provider, never treated as an inline endpoint. Calls are idempotent per `execution_id` in the gateway-owned in-memory cache, transport/HTTP/malformed-response failures return failure-safe results, successful calls are audited by plugin and tool name without arguments, and policy denials are audited and raised as boundary errors.

### REST/Webhook connector
For ordinary third-party APIs and inbound notifications.

### Operator-installed extension
Future package model. Must be signed/allow-listed before loading in production.

## 5. Initial integration catalog
### AI
OpenAI, Anthropic, Google; optional Azure/Bedrock; local vLLM/Ollama.

### Media
FFmpeg/ffprobe, transcription adapter, TTS adapter, image/video generation adapters, licensed stock-media adapters.

### Discovery
Search provider adapters, RSS/feeds, platform discovery APIs where permitted, manually configured sources, trend-data providers.

### Publishing
YouTube Data API, TikTok Content Posting API, Meta Graph/Instagram publishing capabilities subject to account/app eligibility.

### Business
Affiliate networks, URL shortener/link attribution, email/notification provider, optional Stripe for future SaaS billing.

### Infrastructure
S3-compatible object storage, SMTP/email, error tracking, observability, GitHub/Codex engineering workflow.

## 6. Connector security
- OAuth tokens encrypted at rest via secret manager/ref layer.
- Refresh tokens never logged.
- Scope inventory stored and displayed.
- Connector refuses operation when required scope missing.
- Credential expiry/refresh health monitored.
- Per-account kill switch.
- Webhook signatures verified and replay-protected.

## 7. Tool sandboxing
LLM-facing code/shell tools must run inside constrained sandboxes with:
- no host Docker socket;
- no unrestricted host filesystem;
- explicit writable workspace;
- network allowlist;
- CPU/memory/time limits;
- no production secrets unless a narrowly scoped action requires them;
- complete invocation audit.

Implemented in `services/api/clipos_api/tool_policy.py` (CLIP-029): `ToolPolicyEngine` composes the Tool Registry capability check with an operator-declared `SandboxPolicy` (allowed capability scopes, network access, filesystem scopes, and a maximum call count per injectable time window). Capability scopes must be covered by the sandbox, tool metadata requesting network or filesystem access must fit the declared sandbox, disabled/unknown/ungranted tools and sandbox violations are denied, and denials are audited as `tool_policy.denied` while executed calls are audited as `tool_policy.call_recorded` without argument content. Rate-limit state is in-memory with an injectable clock and requires no migration; `build_tool_policy` is the production factory and existing gateway/MCP paths are not force-wired by this ticket.
