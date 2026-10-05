# ClipOS

ClipOS is a self-hosted autonomous digital-media operating system. This repository is prepared for a **VPS-hosted development workflow with Hermes Agent as the build orchestrator and Codex as the primary coding engine**.

## Start here
1. `00_README_FIRST.md`
2. `15_HERMES_ORCHESTRATOR.md`
3. `16_VPS_GITHUB_RUNBOOK.md`
4. `AGENTS.md`
5. `BUILD_STATE.json`
6. `10_TICKETS.yaml`
7. `HERMES_START_PROMPT.md`

`MASTER_CODEX_PROMPT.md` is the contract Hermes gives to Codex workers. Hermes is the single build orchestrator unless the operator explicitly changes that policy.

## Current starting state
The build begins with **M-1 — VPS / Repository / Orchestrator Preflight**. Application work in M0 must not start until M-1 exit gates pass.
