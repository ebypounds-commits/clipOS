# External Integrations & Third-Party Services

## Integration contract
Every provider adapter must implement:
- credential/scopes validation;
- health check;
- explicit supported capabilities;
- normalized errors;
- timeouts;
- rate-limit strategy;
- retries only for safe/transient operations;
- idempotency where possible;
- audit fields;
- mock/fake implementation for CI;
- integration tests behind opt-in credentials.

## YouTube
Use YouTube Data API for uploads/metadata/status and analytics APIs where eligible. Support resumable upload. Unverified API projects may have public-upload restrictions, so production readiness includes required provider audit/approval. Store channel/account linkage and OAuth scopes.

## TikTok
Use Content Posting API for direct post or draft/upload as configured. Respect creator-info requirements, user authorization, `video.publish`/`video.upload` scopes, per-token/provider rate limits, and audit restrictions. Support status polling and webhooks. Honor AI-generated-content labeling fields when applicable.

## Instagram/Facebook
Implement Meta adapters against currently supported Graph/Instagram publishing capabilities at build time. Codex must verify current official Meta documentation, required Page/Instagram account types, permissions, app review requirements, media processing/status flows, and API version before implementation. Do not guess endpoint versions.

## Discovery/search
Use licensed/official/public search and trend sources. Avoid unauthorized scraping where provider terms disallow it. Any browser/computer-use fallback must be explicit, rate-limited, and policy-reviewed; it is not a substitute for an official write API.

## Source ingestion
Allowed source classes:
- uploads owned by operator;
- creator-authorized sources;
- licensed libraries;
- public-domain/permissively licensed sources;
- provider APIs allowing retrieval;
- generated assets.

Do not build DRM bypass. Generic URL download tooling, if present, is disabled by default and restricted to sources for which policy explicitly permits retrieval.

## Notifications
Support email/webhook notifications initially. Future Slack/Teams/Discord adapters may be plugins.

## Optional service matrix
- LLM: OpenAI / Anthropic / Google / local.
- STT: local Whisper-compatible or cloud provider.
- TTS: OpenAI/ElevenLabs/Azure/etc. behind adapter.
- Generated images/video: provider adapters.
- Stock: licensed-provider adapters.
- Storage: MinIO/R2/S3 compatible.
- Error tracking: Sentry optional.
- CDN: Cloudflare optional.
- Email: SMTP/transactional email provider.
- Affiliate networks: adapter-per-network.

## Integration onboarding UI
For each connector show:
- status;
- scopes/capabilities;
- credential expiration;
- last successful call;
- last error;
- rate-limit/quota summary;
- test connection;
- private-test capability;
- public-enabled state;
- kill switch.
