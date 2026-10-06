# Codex Task Packet — CLIP-005

## Lease
- ticket_id: CLIP-005
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-005
- worktree: /srv/clipos/worktrees/CLIP-005
- lease_expires_at: +60 minutes from dispatch

## Objective
Create autonomous ticket runner conventions: codify HOW autonomous runners execute tickets (the loop the orchestrator actually runs) as a canonical conventions document, a machine-enforced task-packet validator, and an append-only operator-visible audit of material ticket/state transitions — each testable and wired into CI.

## Environment constraints
- Sandbox: NO network, NO docker — pip/npm/container gates are SKIP+reason (orchestrator re-runs outside). python3 stdlib available.
- OCCUPIED ports: 22 53 80 443 3000 3080 3100 5432 6001 6002 8000 8080 8090 8642 8776 8888 9119 9377 11235 18080.

## Required reading
15_HERMES_ORCHESTRATOR.md (the source conventions), AGENTS.md, docs/M-1_ORCHESTRATOR_OPERATIONS.md (executed procedure), .agent/CODEX_TASK_PACKET_TEMPLATE.md, scripts/agent_lease.py + scripts/validate_state.py (existing mechanisms), docs/adr/0004-ci-gates-and-deferral-policy.md, docs/development.md, BUILD_STATE.json, 10_TICKETS.yaml.

## Deliverables (exact paths — orchestrator verifies)
1. `.agent/RUNNER_CONVENTIONS.md` — the canonical conventions, derived from 15 + the executed M-1 procedure, covering: lifecycle (lease acquire→worktree→packet→dispatch→ground-truth→verify→land→merge→CI green→evidence→state advance→lease release); single-lease exclusivity; ground-truth rule (session/function outputs or orchestrator-gated re-runs are the only evidence; model-claimed output is never evidence); dispatch failure signatures + retry policy (max attempts, artifact-keyed success); escalation to human gates (secret/cost/destructive/suppression); state-mutation rules (validate_handoff + validate_state after every mutation, evidence fields must cite verifiable facts: commit sha / CI run id / test counts). Reference this file from AGENTS.md (single added line) and docs/development.md.
2. `scripts/validate_packet.py` — stdlib-only task-packet validator: requires top heading `# Codex Task Packet`, sections `## Lease`, `## Objective`, `## Acceptance` (or `## Acceptance ...` heading containing "Acceptance"), `## Completion output`, and Lease sub-keys ticket_id + orchestrator_run_id; rejects empty sections; exit 0 prints `packet: valid (<path>)`, exit 1 prints every missing piece. Subcommand-style usage: `validate_packet.py <path> [<path>...]`.
3. `scripts/state_audit.py` — append-only JSONL audit at `docs/reports/STATE_AUDIT.jsonl` (tracked): `append <ticket> <commit_sha>` writes {at (UTC ISO), ticket, commit_sha, actor:"hermes-clipos-m1-001"} and is idempotent (existing ticket entry → no-op, exit 0, prints `exists`); `verify` exits 0 only if EVERY ticket in BUILD_STATE.completed_tickets has an entry (prints `audit: N/N covered`), else exit 1 listing missing; `bootstrap` creates entries for completed tickets lacking one, using the ticket's `evidence` commit sha if parseable (regex [0-9a-f]{7,40}) else "unknown" (idempotent). Never rewrite existing lines.
4. `services/api/tests/test_runner_conventions.py` — pytest: validate_packet passes on .agent/CODEX_TASK_PACKET_TEMPLATE.md AND on .agent/task_packet_CLIP-005.md (this very packet); fails (exit 1) on a temp packet missing Lease/Acceptance; state_audit append idempotence (tmp JSONL via env override — support `STATE_AUDIT_PATH` env so tests never touch the real file); verify bootstrap behavior on a temp audit + temp BUILD_STATE (env override `BUILD_STATE_PATH` for the tests... or design functions taking paths — prefer importable functions with path params, CLI thin wrapper). No network.
5. CI wiring: `validate` job step after validate_state/architecture_guard: `python3 scripts/validate_packet.py .agent/CODEX_TASK_PACKET_TEMPLATE.md && python3 scripts/state_audit.py verify`. NOTE: verify will fail until bootstrap ran — run `python3 scripts/state_audit.py bootstrap` as part of THIS ticket's deliverable (with the real repo state) so CI passes.
6. Makefile: add the two commands to the `governance` target (or a `conventions` target chained into it).
7. docs/development.md: "Autonomous runner" subsection linking .agent/RUNNER_CONVENTIONS.md, validate_packet, state audit (and how operators inspect `docs/reports/STATE_AUDIT.jsonl`).
8. `.agent/dryrun/CLIP-005_completion_report.json` per schema.

## Acceptance (orchestrator re-runs outside sandbox)
- python3 scripts/validate_packet.py on template + this packet → exit 0; broken fixture → exit 1 (demonstrate in report).
- python3 scripts/state_audit.py verify → exit 0 covering all 10 completed tickets after bootstrap; append twice → single entry.
- pytest: all prior 21 + new runner-convention tests green; ruff format --check + ruff check (services/api scope) + mypy clean; compileall green.
- Workflow YAML valid; validate job contains the new line; report still needs all four jobs.
- validate_handoff + validate_state + architecture_guard + secret_scan all green.
- AGENTS.md/docs touched ONLY by additive links (no content removal).

## Allowed scope
paths: .agent/RUNNER_CONVENTIONS.md, .agent/CODEX_TASK_PACKET_TEMPLATE.md (only if a real gap exists), scripts/validate_packet.py, scripts/state_audit.py, services/api/tests/test_runner_conventions.py, docs/reports/STATE_AUDIT.jsonl, docs/development.md, AGENTS.md (additive link line), .github/workflows/baseline-ci.yml, Makefile, .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/* other than STATE_AUDIT.jsonl; weakening CI; deleting existing audit content; fabricating results (SKIP+reason).

## Required gates (runnable here)
- python3 scripts/validate_packet.py .agent/CODEX_TASK_PACKET_TEMPLATE.md .agent/task_packet_CLIP-005.md
- python3 scripts/state_audit.py verify (after bootstrap)
- python3 -m pytest -q services/api (if importable else SKIP)
- python3 -m compileall scripts services/api
- python3 scripts/validate_handoff.py; python3 scripts/validate_state.py; python3 scripts/architecture_guard.py; bash scripts/secret_scan.sh
- yaml parse of .github/workflows/baseline-ci.yml

## Completion output
Completion report + concise summary; no fabrication.
