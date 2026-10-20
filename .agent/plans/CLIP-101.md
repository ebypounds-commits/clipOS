# CLIP-101 — Autonomy evidence and operations manual (M24)

## Goal

M24's exit gate pairs *autonomy evidence* with the operator runbook, and
PRD §7 requires production-readiness evidence before `AUTONOMOUS_PUBLIC`.
The autonomous build already recorded its claims — `BUILD_STATE.json`
(tickets, evidence, milestones), `STATE_AUDIT.jsonl` (transition coverage),
the qualification harnesses — but nothing checked those claims **against
each other**. This ticket adds that check and consolidates the scattered
operator documentation into one manual.

## Deliverables

| Path | Purpose |
| --- | --- |
| `services/api/clipos_api/autonomy_evidence.py` | Typed inputs + pure report assembler |
| `scripts/autonomy_evidence.py` | CLI gathering the real record, CI probe, JSON report |
| `services/api/tests/test_autonomy_evidence.py` | 23 tests |
| `docs/OPERATIONS_MANUAL.md` | Consolidated operator manual |
| `BUILD_STATE.json` (corrected) | `tickets_total` 102 → 108 |

## The evidence checks

| ID | Check | Fails when |
| --- | --- | --- |
| E1 | Ticket ledger | counter ≠ ledger size, duplicates, completed > total |
| E2 | Audit coverage | `state_audit.py` coverage ≠ completed count (UNKNOWN → WARN) |
| E3 | Evidence trail | empty, or last entry names an incomplete ticket |
| E4 | Milestone ledger | current milestone already in completed list |
| E5 | Operating mode | not one of the five PRD §7 modes |
| E6 | Publishing posture | `public_publishing=true` while the `first_public_post` gate is closed; production deploy warns |
| E7 | Human gates | any documented gate undeclared (open gates are facts, not faults) |
| E8 | Harness inventory | any qualification harness missing from the tree |
| E9 | CI record | last recorded run not green on HEAD (pending → WARN) |

The honesty rule throughout: an uncheckable claim is reported as UNKNOWN and
flagged, never silently as healthy — the same rule CLIP-100 applied to
insufficient SLO data.

## The check that bit immediately

The first run against the real record **failed**: `tickets_completed=106`
against `tickets_total=102`. The six P001–P006 pre-tickets were folded into
the ledger when the project started but the declared total was never
updated; `validate_handoff` has counted 108 ids in `10_TICKETS.yaml` all
along, and nothing asserted the total because no script read it. Corrected
to 108 — this is the harness doing its job on day one.

A parser bug was also caught by the suite: `state_audit.py` prints
`audit: 106/106 covered`, and a bare `isdigit()` scan of the tokens never
matches `106/106`, so coverage would have been reported UNKNOWN forever.
The parser now reads the fraction directly.

## Operations manual

`docs/OPERATIONS_MANUAL.md` consolidates the operator surface into one map:
health/status endpoints, the nine verification commands, deployment
lifecycle (bootstrap → canary → promote/rollback), publishing governance and
the owner gate, incidents/remediation/drills, cost control, backups, secrets
and rotation, the human gates, and an explicit closing section on what the
automation does not do.

## Verification

- 23 new tests, including deliberate contradictions (doctored ledger fails,
  public-without-gate fails, missing harness fails) and CLI runs against the
  repository's real record.
- 1825 passed CI-env (94.04% cov), 1 skipped.
- ruff clean (API + script); mypy clean.
- secret_scan clean; architecture_guard 6/6; deploy_topology_check PASS.
- No migration, no new API routes: evidence derives from existing state.
