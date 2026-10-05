# Codex Task Packet

## Lease
- ticket_id: CLIP-P005
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-P005
- worktree: /srv/clipos/worktrees/CLIP-P005
- lease_expires_at: +60 minutes from dispatch

## Objective
Bootstrap the minimum baseline CI lane plus a secrets-hygiene preflight for this repository. Two new files only.

## Acceptance criteria
- `.github/workflows/baseline-ci.yml` exists and is valid YAML (parse it with python3 `yaml.safe_load`).
- Workflow triggers on `push` to `main` and on `pull_request` targeting `main`.
- Workflow has job `validate` (ubuntu-latest) that, after `actions/checkout@v4` and `actions/setup-python@v5` with python-version '3.12', runs in order:
  1. `pip install pyyaml`
  2. `python3 scripts/validate_handoff.py`
  3. `bash scripts/preflight.sh`
  4. `python3 scripts/test_agent_lease.py`
  5. `bash scripts/secret_scan.sh`
- Workflow has job `report` with `needs: validate` and `if: always()` that writes the result to branch `ci-results` using a single commit containing file `last_run.txt` with lines: `status=<success|failure>`, `run=${{ github.run_id }}`, `sha=${{ github.sha }}`, pushed with `git -c user.name=ci-bot -c user.email=ci-bot@clipos.local commit` (use the default GITHUB_TOKEN via actions/checkout with `fetch-depth: 0` and a remote URL embedding the token; if push fails, the job must fail). This is the interim status channel until an API token exists.
- `scripts/secret_scan.sh` exists, is bash, `set -euo pipefail`, and:
  - Fails (exit 1) if any tracked file matches the path pattern `(^|/)(\.env($|\.)|.*\.(pem|key|p12|pfx)$)` (use `git ls-files`).
  - Fails (exit 1) if any tracked TEXT file (skip binaries via `grep -rIl`) contains a real-looking secret: `sk-[A-Za-z0-9]{20,}`, `sk-or-v1-[A-Za-z0-9]{8,}`, `ghp_[A-Za-z0-9]{36}`, `github_pat_[A-Za-z0-9_]{22,}`, `AKIA[0-9A-Z]{16}`, or `-----BEGIN [A-Z ]*PRIVATE KEY-----`.
  - Prints `secret_scan: clean` and exits 0 when nothing found.
  - MUST pass against the current repository state (run it in the worktree).
- All gates pass IN THE WORKTREE before commit: yaml parse of the workflow, `bash scripts/secret_scan.sh` (exit 0), `python3 scripts/validate_handoff.py`, `python3 scripts/test_agent_lease.py`.
- Commit everything on branch `agent/CLIP-P005` (identity configured). Do NOT push, do NOT touch main, BUILD_STATE.json or 10_TICKETS.yaml.

## Required reading
AGENTS.md, MASTER_CODEX_PROMPT.md, .agent/PLANS.md, 17_M_MINUS_1_PREFLIGHT.md (preflight requirements), scripts/preflight.sh

## Allowed scope
- paths: .github/workflows/baseline-ci.yml, scripts/secret_scan.sh, .agent/** (packet may remain)
- migrations allowed: no
- external network actions allowed: no (no gh api calls, no pushes)

## Forbidden actions
- do not merge to main / do not push
- do not select unrelated tickets
- do not expose secrets
- do not change human-gate policy
- do not modify another active worktree

## Required gates
- python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/baseline-ci.yml'))"
- bash scripts/secret_scan.sh → exit 0
- python3 scripts/validate_handoff.py → OK line
- python3 scripts/test_agent_lease.py → RESULT: all passed

## Completion output
Write `.agent/dryrun/p005_completion_report.json` with ticket_id, status, branch, worktree, summary, files_changed, tests[], commit_shas, working_tree_clean, blockers (schema: schemas/codex_completion_report.schema.json), then print a concise summary.
