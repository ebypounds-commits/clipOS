# Agent Registry Specification

## Goal
Every autonomous specialist is data-configured. Application code defines safe capabilities; the registry determines which specialist may use which capabilities, with which logical model, budget, memory, evaluation, and escalation policy.

## Agent classes
- `research`
- `planning`
- `creative`
- `media_analysis`
- `compliance`
- `publishing`
- `analytics`
- `finance`
- `operations`
- `governance`
- `engineering_proposal`
- `evaluation`

## Required initial agents
1. Governor
2. Portfolio Manager
3. Trend Discovery
4. Niche/Opportunity Analyst
5. Source Discovery
6. Rights Analyst
7. Research/Fact Agent
8. Content Planner
9. Hook Agent
10. Script Transformation Agent
11. Clip Candidate Agent
12. Media QC Agent
13. Caption Agent
14. Visual Enrichment Planner
15. Metadata/Packaging Agent
16. Compliance Agent
17. Publishing Supervisor
18. Analytics Agent
19. Growth/Experiment Agent
20. Revenue Attribution Agent
21. Finance/Budget Agent
22. Health Supervisor
23. Incident Triage Agent
24. Improvement Proposal Agent
25. Agent Architect
26. Evaluation Agent

## Agent version schema
Each immutable `agent_version` contains:
- name, purpose, class;
- instruction/prompt version reference;
- logical model alias and fallback aliases;
- required capabilities: reasoning, vision, structured output, context size, tool use;
- tool grants;
- prohibited tools/actions;
- memory policy;
- max iterations/turns;
- max tool calls;
- timeout;
- per-run, hourly, daily, monthly spend ceilings;
- concurrency ceiling;
- evaluator(s) and required score;
- retry policy;
- escalation policy;
- risk classification;
- activation state and canary percentage.

## Permission model
Tools expose capability strings. Agents receive explicit grants, e.g.:
- `content.read`
- `content.write`
- `rights.read`
- `rights.recommend`
- `publish.prepare`
- `publish.execute`
- `analytics.read`
- `finance.read`
- `budget.request`
- `server.read_health`
- `incident.create`
- `engineering.propose`

No LLM agent receives `secrets.read_raw`, unrestricted shell, production database superuser, unrestricted filesystem, or unrestricted financial transfer permissions.

## Memory
Memory scopes:
- run-local;
- workflow;
- brand;
- agent-specialist;
- organization learning;
- global system rules (read-only to most agents).

Memory writes pass through a memory service with provenance and versioning. Agents must not directly mutate arbitrary vector collections.

## Routing
The registry resolves:
`agent_id -> active agent_version -> logical model alias -> routing policy -> provider/model endpoint`.

Fallback occurs only when semantic equivalence is acceptable. A model failure during a deterministic compliance decision may trigger re-evaluation rather than blindly accepting a fallback result.

## Evaluations
Every production agent requires:
- deterministic schema validation;
- task-specific eval suite;
- cost ceiling;
- latency ceiling where relevant;
- forbidden-behavior tests;
- regression dataset;
- minimum pass threshold;
- canary before promotion.

## Activation lifecycle
`DRAFT -> EVALUATING -> CANARY -> ACTIVE -> SUSPENDED -> RETIRED`.

## Agent Factory rule
The Agent Architect may propose agent definitions but cannot activate them. Automated activation is allowed only after all evaluation gates and policy checks pass, and only for low/medium-risk agent classes. High-risk agents require human approval.
