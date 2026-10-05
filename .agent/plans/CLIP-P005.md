# CLIP-P005 ExecPlan

- Ticket/objective: baseline CI lane plus secrets-hygiene preflight using exactly two new implementation files.
- Current facts: branch `agent/CLIP-P005`; prior M-1 scripts exist; no baseline workflow or secret scanner yet.
- Scope: `.github/workflows/baseline-ci.yml`, `scripts/secret_scan.sh`; evidence/plan under `.agent/`.
- Non-scope: no pushes, no main changes, no BUILD_STATE/10_TICKETS edits, no migrations, no network actions.
- Security impact: detects likely leaked provider/API/private-key secrets among tracked text files and blocks secret-like tracked paths; no credential changes.
- Steps: implement scanner, implement YAML workflow, run local YAML/secret/validate/lease gates, write completion evidence, commit.
- Gate plan: PyYAML parse; scanner exit 0; `validate_handoff.py`; `test_agent_lease.py`; inspect final diff/status.
- Rollback: revert the branch commit; no shared/global state is changed.
- Evidence: `.agent/dryrun/p005_completion_report.json` and branch commit SHA.
