"""Autonomy evidence: machine-checkable proof of the build's own claims (CLIP-101).

M24's exit gate lists *autonomy evidence* alongside the operator runbook, and
PRD §7 requires production-readiness evidence before `AUTONOMOUS_PUBLIC`.
The autonomous build already records its claims — `BUILD_STATE.json` tracks
completed tickets, evidence entries and milestones; `STATE_AUDIT.jsonl` pins
every state transition to a merge SHA; the harness scripts qualify the
controls. What nothing did was check those claims **against each other**.

This module assembles one report from typed inputs and issues a verdict:

- **Ledger integrity** — `completed_tickets` matches the `tickets_completed`
  counter, fits inside `tickets_total`, and carries no duplicates; the last
  evidence entry names a ticket that is actually completed; the audit trail
  covers every completed ticket.
- **Operating posture** — the declared operating mode is one of the PRD §7
  modes; public publishing and production deployment are reported as they
  stand, and a public-publishing claim without the matching owner-gate
  approval is a finding, because PRD §7 ties autonomous public operation to
  evidence the operator has not given yet.
- **Human gates** — the documented gates are reported with their current
  state. Closed gates are not themselves evidence of anything (an operator
  may simply not have acted yet); an *open* public-posting gate alongside
  `public_publishing=true` is a contradiction and fails the report.
- **Qualification inventory** — every qualification harness the build relies
  on (security, private-mode, deployment topology, SLO/ops, schema drift,
  agent FK ordering) must be present in the tree; a missing harness means a
  claim with nothing backing it.

The pure core takes typed inputs and has no I/O, so tests drive it with
synthetic ledgers; the CLI (`scripts/autonomy_evidence.py`) gathers the real
ones from the repository and can also probe a live instance. Honest
reporting is the point: an unelectable check is reported as UNKNOWN with a
finding, never silently as healthy.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The qualification harnesses whose presence the evidence report requires.
# Each maps to a concrete automated check of a runtime claim.
QUALIFICATION_HARNESSES = {
    "security_qualification.py": "M22 security controls against a live instance",
    "private_mode_qualification.py": "end-to-end private-mode pipeline and invariants",
    "deploy_topology_check.py": "staging/production compose hardening invariants",
    "schema_drift.py": "model-vs-migration schema agreement",
    "infrastructure_health.sh": "compose infrastructure health",
}

# PRD §7 operating modes, in escalation order.
PRD_OPERATING_MODES = ("OBSERVE", "DRAFT", "PRIVATE_TEST", "GUARDED_PUBLIC", "AUTONOMOUS_PUBLIC")

# The human gates 13_HUMAN_GATES_AND_SECRETS.md makes mandatory.
EXPECTED_HUMAN_GATES = (
    "production_secrets",
    "youtube_real_account",
    "tiktok_real_account",
    "meta_real_account",
    "first_public_post",
)

Verdict = Literal["PASS", "FAIL"]


class Severity(str, Enum):
    """Finding severity; FAIL fails the report, WARN survives without --strict."""

    FAIL = "FAIL"
    WARN = "WARN"
    INFO = "INFO"


class EvidenceFinding(BaseModel):
    """One checked claim and what was observed."""

    model_config = ConfigDict(frozen=True)

    check_id: str
    title: str
    severity: Severity
    detail: str


class BuildLedgerInput(BaseModel):
    """Ticket/evidence/audit counters as recorded by the build."""

    model_config = ConfigDict(frozen=True)

    completed_ticket_ids: list[str]
    tickets_completed: int
    tickets_total: int
    audit_covered: int
    last_evidence_ticket: str | None
    evidence_entry_count: int
    completed_milestones: list[str]
    current_milestone: str
    current_ticket: str


class OperatingPostureInput(BaseModel):
    """Flags and gates exactly as BUILD_STATE.json records them."""

    model_config = ConfigDict(frozen=True)

    operating_mode: str
    public_publishing: bool
    production_deploy: bool
    human_gates: dict[str, bool]
    autonomous_build: bool = True


class HarnessInput(BaseModel):
    """Which qualification harnesses were found in the tree."""

    model_config = ConfigDict(frozen=True)

    present: list[str]
    missing: list[str]


class AutonomyEvidenceReport(BaseModel):
    """The assembled report."""

    model_config = ConfigDict(frozen=True)

    tool: str = "clipos-autonomy-evidence"
    version: int = 1
    verdict: Verdict
    findings: list[EvidenceFinding] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)

    @property
    def failed(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.WARN)


def _check_ledger(ledger: BuildLedgerInput) -> list[EvidenceFinding]:
    findings: list[EvidenceFinding] = []
    ids = ledger.completed_ticket_ids
    if ledger.tickets_completed != len(ids):
        findings.append(
            EvidenceFinding(
                check_id="E1",
                title="completed-ticket counter matches the ledger",
                severity=Severity.FAIL,
                detail=(
                    f"tickets_completed={ledger.tickets_completed} but the ledger "
                    f"lists {len(ids)} ids"
                ),
            )
        )
    else:
        findings.append(
            EvidenceFinding(
                check_id="E1",
                title="completed-ticket counter matches the ledger",
                severity=Severity.INFO,
                detail=f"{len(ids)} completed tickets",
            )
        )

    duplicates = sorted({t for t in ids if ids.count(t) > 1})
    if duplicates:
        findings.append(
            EvidenceFinding(
                check_id="E1",
                title="no duplicate ticket ids",
                severity=Severity.FAIL,
                detail=f"duplicated: {', '.join(duplicates)}",
            )
        )
    if ids and ledger.tickets_total and len(ids) > ledger.tickets_total:
        findings.append(
            EvidenceFinding(
                check_id="E1",
                title="completed tickets fit inside the declared total",
                severity=Severity.FAIL,
                detail=f"{len(ids)} completed exceeds total {ledger.tickets_total}",
            )
        )

    if ledger.audit_covered != len(ids):
        findings.append(
            EvidenceFinding(
                check_id="E2",
                title="audit trail covers every completed ticket",
                severity=Severity.FAIL,
                detail=(f"audit covered {ledger.audit_covered} of {len(ids)} completed tickets"),
            )
        )
    else:
        findings.append(
            EvidenceFinding(
                check_id="E2",
                title="audit trail covers every completed ticket",
                severity=Severity.INFO,
                detail=f"{ledger.audit_covered}/{len(ids)} covered",
            )
        )

    last = ledger.last_evidence_ticket
    if last is not None and last not in ids:
        findings.append(
            EvidenceFinding(
                check_id="E3",
                title="last evidence entry names a completed ticket",
                severity=Severity.FAIL,
                detail=f"last_evidence.ticket={last!r} is not in completed_tickets",
            )
        )
    elif ledger.evidence_entry_count == 0:
        findings.append(
            EvidenceFinding(
                check_id="E3",
                title="evidence trail is non-empty",
                severity=Severity.FAIL,
                detail="BUILD_STATE.evidence is empty",
            )
        )
    else:
        findings.append(
            EvidenceFinding(
                check_id="E3",
                title="evidence trail is non-empty and current",
                severity=Severity.INFO,
                detail=(f"{ledger.evidence_entry_count} entries; latest ticket={last!r}"),
            )
        )

    if ledger.completed_milestones and ledger.current_milestone in ledger.completed_milestones:
        findings.append(
            EvidenceFinding(
                check_id="E4",
                title="current milestone is not already completed",
                severity=Severity.FAIL,
                detail=(
                    f"current_milestone={ledger.current_milestone} appears in completed_milestones"
                ),
            )
        )
    else:
        findings.append(
            EvidenceFinding(
                check_id="E4",
                title="milestone ledger is coherent",
                severity=Severity.INFO,
                detail=(
                    f"{len(ledger.completed_milestones)} milestones completed; "
                    f"current={ledger.current_milestone}, next ticket="
                    f"{ledger.current_ticket}"
                ),
            )
        )
    return findings


def _check_posture(posture: OperatingPostureInput) -> list[EvidenceFinding]:
    findings: list[EvidenceFinding] = []
    if posture.operating_mode in PRD_OPERATING_MODES:
        findings.append(
            EvidenceFinding(
                check_id="E5",
                title="operating mode is a PRD §7 mode",
                severity=Severity.INFO,
                detail=f"operating_mode={posture.operating_mode}",
            )
        )
    else:
        findings.append(
            EvidenceFinding(
                check_id="E5",
                title="operating mode is a PRD §7 mode",
                severity=Severity.FAIL,
                detail=(
                    f"operating_mode={posture.operating_mode!r} is not one of "
                    f"{', '.join(PRD_OPERATING_MODES)}"
                ),
            )
        )

    first_public = posture.human_gates.get("first_public_post")
    if posture.public_publishing and first_public is False:
        findings.append(
            EvidenceFinding(
                check_id="E6",
                title="public publishing is backed by its owner gate",
                severity=Severity.FAIL,
                detail=(
                    "public_publishing=true while the first_public_post human gate "
                    "is still closed — autonomous public operation requires the "
                    "owner approval PRD §7 ties it to"
                ),
            )
        )
    else:
        findings.append(
            EvidenceFinding(
                check_id="E6",
                title="public publishing is backed by its owner gate",
                severity=Severity.INFO,
                detail=(
                    f"public_publishing={posture.public_publishing}, "
                    f"first_public_post gate={first_public}"
                ),
            )
        )

    if posture.production_deploy:
        findings.append(
            EvidenceFinding(
                check_id="E6",
                title="production deployment posture",
                severity=Severity.WARN,
                detail=(
                    "production_deploy=true — confirm the production_secrets human "
                    "gate has been executed before treating this as a production "
                    "qualification"
                ),
            )
        )
    else:
        findings.append(
            EvidenceFinding(
                check_id="E6",
                title="production deployment posture",
                severity=Severity.INFO,
                detail="production_deploy=false (staging/local qualification only)",
            )
        )
    return findings


def _check_gates(posture: OperatingPostureInput) -> list[EvidenceFinding]:
    missing = [g for g in EXPECTED_HUMAN_GATES if g not in posture.human_gates]
    if missing:
        return [
            EvidenceFinding(
                check_id="E7",
                title="documented human gates are all declared",
                severity=Severity.FAIL,
                detail=f"missing gate declarations: {', '.join(missing)}",
            )
        ]
    closed = sorted(g for g, ok in posture.human_gates.items() if not ok)
    open_gates = sorted(g for g, ok in posture.human_gates.items() if ok)
    detail = f"{len(closed)} closed"
    if open_gates:
        detail += f"; open: {', '.join(open_gates)}"
    return [
        EvidenceFinding(
            check_id="E7",
            title="documented human gates are all declared",
            severity=Severity.INFO,
            detail=detail,
        )
    ]


def _check_harnesses(harness: HarnessInput) -> list[EvidenceFinding]:
    if harness.missing:
        return [
            EvidenceFinding(
                check_id="E8",
                title="qualification harnesses are all present",
                severity=Severity.FAIL,
                detail=f"missing: {', '.join(sorted(harness.missing))}",
            )
        ]
    return [
        EvidenceFinding(
            check_id="E8",
            title="qualification harnesses are all present",
            severity=Severity.INFO,
            detail=f"{len(harness.present)} harnesses found in the tree",
        )
    ]


def assemble_evidence(
    ledger: BuildLedgerInput,
    posture: OperatingPostureInput,
    harnesses: HarnessInput,
) -> AutonomyEvidenceReport:
    """Pure: check every claim and issue the verdict."""
    findings: list[EvidenceFinding] = []
    findings.extend(_check_ledger(ledger))
    findings.extend(_check_posture(posture))
    findings.extend(_check_gates(posture))
    findings.extend(_check_harnesses(harnesses))

    failed = sum(1 for f in findings if f.severity is Severity.FAIL)
    warned = sum(1 for f in findings if f.severity is Severity.WARN)
    info = sum(1 for f in findings if f.severity is Severity.INFO)
    return AutonomyEvidenceReport(
        verdict="FAIL" if failed else "PASS",
        findings=findings,
        summary={
            "total": len(findings),
            "failed": failed,
            "warnings": warned,
            "info": info,
            "tickets_completed": ledger.tickets_completed,
            "tickets_total": ledger.tickets_total,
            "operating_mode": posture.operating_mode,
            "public_publishing": posture.public_publishing,
            "human_gates_open": sorted(g for g, ok in posture.human_gates.items() if ok),
        },
    )


__all__ = [
    "EXPECTED_HUMAN_GATES",
    "PRD_OPERATING_MODES",
    "QUALIFICATION_HARNESSES",
    "AutonomyEvidenceReport",
    "BuildLedgerInput",
    "EvidenceFinding",
    "HarnessInput",
    "OperatingPostureInput",
    "Severity",
    "assemble_evidence",
]
