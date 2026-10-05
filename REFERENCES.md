# Current Reference Notes (verify again at implementation time)

- OpenAI Agents SDK: code-first agents when the application owns deployment, tools, state, approvals, and runtime behavior: https://developers.openai.com/api/docs/guides/agents/sdk
- OpenAI agent integrations/observability and MCP/tracing: https://developers.openai.com/api/docs/guides/agents/integrations-observability
- OpenAI Codex + Agents SDK workflow example: https://developers.openai.com/cookbook/examples/codex/codex_mcp_agents_sdk/building_consistent_workflows_codex_cli_agents_sdk
- OpenAI Codex ExecPlans/AGENTS guidance: https://developers.openai.com/cookbook/articles/codex_exec_plans
- YouTube `videos.insert`: https://developers.google.com/youtube/v3/docs/videos/insert
- TikTok Content Posting Direct Post: https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post
- TikTok Get Started: https://developers.tiktok.com/docs/en/content-posting-api-get-started
- Temporal self-hosted guide: https://docs.temporal.io/self-hosted-guide

Provider docs change. Codex must verify official documentation at the time each connector is implemented and record API versions/scopes/audit requirements in an ADR/integration doc.

## Hermes Agent orchestration references added for VPS build v2
- Hermes Agent repository: https://github.com/hermes-agent-org/hermes
- Hermes Agent documentation: https://hermes-agent.nousresearch.com/docs/
- Hermes Agent tools/toolsets: https://hermes-agent.nousresearch.com/docs/user-guide/features/tools
- Hermes Agent quickstart / MCP / editor integration: https://hermes-agent.nousresearch.com/docs/getting-started/quickstart/

These references describe current Hermes capabilities used as orchestration aids (delegation, memory/skills, cron, MCP, terminal/file tooling). ClipOS does not depend on Hermes memory as authoritative state.
