# Codex Task Packet — CLIP-004

## Lease
- ticket_id: CLIP-004
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-004
- worktree: /srv/clipos/worktrees/CLIP-004
- lease_expires_at: +60 minutes from dispatch

## Objective
Create architecture/ADR/build-state governance: a durable ADR process with the founding records, machine-checkable architecture rules, and a formal schema + validator for BUILD_STATE.json, wired into CI and tests.

## Environment constraints
- Sandbox: NO network, NO docker — pip/npm/container gates are SKIP+reason (orchestrator re-runs outside). python3 stdlib is available.
- OCCUPIED ports: 22 53 80 443 3000 3080 3100 5432 6001 6002 8000 8080 8090 8642 8776 8888 9119 9377 11235 18080.

## Required reading
02_SYSTEM_ARCHITECTURE.md, 07_SECURITY_GOVERNANCE_RIGHTS.md (governance posture), 11_CI_CD_DEPLOYMENT.md (review model + "never green by suppression"), 15_HERMES_ORCHESTRATOR.md (build-state advancement rules), docs/M-1_ORCHESTRATOR_OPERATIONS.md, AGENTS.md, BUILD_STATE.json (actual current shape), schemas/codex_completion_report.schema.json (house schema style), docs/development.md (CI lane matrix).

## Deliverables (exact paths — orchestrator verifies)
1. `docs/adr/README.md` — the ADR process: when a decision needs an ADR, numbering, status lifecycle (proposed/accepted/superseded/deferred), review requirements per 11 (security reviewer for auth/secrets/infra decisions), link from docs/development.md.
2. `docs/adr/0000-template.md` — template with sections: Title, Status, Date, Context, Decision, Alternatives considered, Consequences, Compliance impact, Related.
3. Four founding ADRs derived STRICTLY from the specs (no invented facts): `0001-modular-monolith-and-worker-processes.md` (02§1), `0002-core-stack-selection.md` (02§2 — table stack item → spec source), `0003-provider-registry-alias-policy.md` (02§7 + 00_README doctrine 6: no production agent hard-codes provider/model; registry.py is the enforcement point), `0004-ci-gates-and-deferral-policy.md` (11 lanes + CLIP-003 lane matrix: implemented vs deferred-with-reason; suppression forbidden without ADR). Status: accepted. Dates: 2026-10-06.
4. `schemas/build_state.schema.json` — JSON Schema (draft-07) for BUILD_STATE.json: required keys current_ticket (string), status (string enum: M-1_PREFLIGHT, M-1_IN_PROGRESS, M0_READY, M0_IN_PROGRESS + allow future M\d+ states via pattern — use `oneOf` enum for known + pattern `^(M\\d+_|M-1_)`), completed_tickets (array of unique strings), milestone (string), last_evidence (object with ticket/at/facts), next_action (string); additionalProperties: true (forward-compat for orchestration additions like human_gates). Match the ACTUAL current file — read it first.
5. `scripts/validate_state.py` — stdlib-only validator: loads BUILD_STATE.json, validates against schemas/build_state.schema.json (implement a pragmatic subset checker in stdlib — no external jsonschema dep — required keys, types, enum/pattern for status, unique completed_tickets), plus cross-checks: current_ticket NOT in completed_tickets; current_ticket exists in 10_TICKETS.yaml; last_evidence.ticket in completed_tickets. Exit 0 + print `state: valid (N completed)` or exit 1 with the exact violation.
6. `scripts/architecture_guard.py` — stdlib-only static rules, exit 0/1 with per-rule output: (a) docs/adr contains README, template and ≥4 numbered ADRs; (b) clipos_api.modules module enum still lists the 21 module names from 02§4; (c) providers.py contains NO literal provider/model identifiers (regex `gpt-|claude-|glm-|deepseek|gemini|llama-\d|mistral|mixtral` must not match); (d) registry.py exposes register/get/clear; (e) .github/workflows/baseline-ci.yml still has jobs validate/test/quality/build and report needing all four; (f) BUILD_STATE.json parses.
7. `services/api/tests/test_governance.py` — pytest covering: schema file parses & marks the real BUILD_STATE valid (invoke validate_state functions), each ADR file exists with Status: accepted, architecture_guard returns 0 when run from repo root (subprocess), status enum rejects a bogus status. No network.
8. CI wiring: add to `validate` job a step after secret scan: `python3 scripts/validate_state.py && python3 scripts/architecture_guard.py`. Keep everything else intact; report job still needs all four.
9. Makefile: add `governance` target running validate_state + architecture_guard + the governance tests; existing targets keep working.
10. `docs/development.md`: short "Architecture & governance" section linking docs/adr/, the state schema, guard command; add lane-matrix rows? Only if trivial — matrix rows for governance already belong to existing lanes (do not churn the matrix beyond adding one line if needed).
11. `.agent/dryrun/CLIP-004_completion_report.json` per schema.

## Acceptance (orchestrator re-runs outside sandbox)
- python3 scripts/validate_state.py exit 0; architecture_guard exit 0; both FAIL when deliberately broken (temporarily mutate a copy — restore; report how you tested).
- pytest: all existing 14+ new governance tests green; ruff format --check + check + mypy clean (extend mypy config if new scripts added under services/api only — scripts/ is stdlib, exclude from mypy if needed via config).
- Workflow YAML valid; validate job contains the new step; report needs all four.
- docs/adr has README + template + 4 ADRs; validate_handoff OK; secret_scan clean.
- 10_TICKETS.yaml/BUILD_STATE.json/docs/reports/* untouched (validate_state READS state only).

## Allowed scope
paths: docs/adr/**, docs/development.md, schemas/build_state.schema.json, scripts/validate_state.py, scripts/architecture_guard.py, services/api/tests/test_governance.py, .github/workflows/baseline-ci.yml, Makefile, services/api/pyproject.toml (mypy/exclusions if needed), .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; weakening CI jobs; inventing facts not in the specs; fabricating test results (SKIP+reason).

## Required gates (runnable here)
- python3 scripts/validate_state.py; python3 scripts/architecture_guard.py
- python3 -m pytest -q services/api (if importable else SKIP)
- python3 -m compileall scripts services/api
- python3 scripts/validate_handoff.py; bash scripts/secret_scan.sh
- yaml parse of .github/workflows/baseline-ci.yml

## Completion output
Completion report + concise summary; no fabrication.
