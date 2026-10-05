# ClipOS — Hermes + Codex VPS Handoff Package

## Purpose
ClipOS is a self-hosted, model-agnostic autonomous digital-media operating system. This package is the source of truth for a build where **Hermes Agent orchestrates** and **Codex implements** on a VPS-connected GitHub repository.

## Build doctrine
1. Build a production system, not a demo.
2. Git/repository state is authoritative; agent memory is not.
3. Hermes is the single build orchestrator by default.
4. Codex is the primary coding worker and stays inside the ticket/worktree assigned by Hermes.
5. Prefer deterministic services and durable workflows over LLM calls when ordinary software is sufficient.
6. Operational LLMs are specialists behind Agent/Model/Tool registries; no production agent hard-codes a provider/model.
7. All external actions are capability-gated, budgeted, auditable, idempotent, retryable and revocable.
8. Rights/provenance must be clear before publication.
9. Official platform APIs are preferred over brittle browser automation.
10. Self-improvement occurs through proposals, tests, canaries and promotion—not uncontrolled self-modification.
11. Human intervention is reserved for true credentials/account-owner actions, legal/financial gates, destructive production actions and serious policy disputes.

## Read order
1. `README.md`
2. `AGENTS.md`
3. `.agent/PLANS.md`
4. `15_HERMES_ORCHESTRATOR.md`
5. `16_VPS_GITHUB_RUNBOOK.md`
6. `17_M_MINUS_1_PREFLIGHT.md`
7. `01_PRODUCT_REQUIREMENTS.md` through `14_TRANSFER_TO_CODEX.md`
8. `09_MILESTONES_M0_M24.md`
9. `10_TICKETS.yaml`
10. `BUILD_STATE.json`
11. `HERMES_START_PROMPT.md`
12. `MASTER_CODEX_PROMPT.md`

## Start command
From the VPS canonical repository, start Hermes and give it the contents of `HERMES_START_PROMPT.md`. Hermes then dispatches Codex using the repository's Codex contract.

## Initial build phase
Start at **M-1**, not M0. M-1 proves GitHub, VPS, Docker, worktrees, Hermes/Codex role separation, secrets hygiene and CI before product code begins.

## First product proof
After the engineering foundation is qualified, the first production proof remains deliberately narrow: rights-safe/original/authorized content; initial brands around AI/productivity, hair/beauty/self-care and useful products/problem-solvers; official YouTube/TikTok/Meta adapters as account approvals permit; owner gate before first public posting per real platform account.
