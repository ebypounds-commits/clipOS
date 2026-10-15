"""Codex engineering-ticket generation/export (CLIP-085, M20 closer).

PRD journey 5 tail — turn an accepted improvement proposal into an
implementation-ready Codex task packet (the hand-off artifact the
autonomous runner already consumes in ``.agent/``):

- **Generation** — ``generate_packet_markdown`` is a pure renderer: it
  builds a full ``# Codex Task Packet`` markdown document with every
  heading the repo validator requires (Lease / Objective / Acceptance
  criteria / Required reading / Allowed scope / Forbidden actions /
  Required gates / Completion output). The objective and acceptance
  criteria derive from the CLIP-082 proposal (weakness_ref + linked
  evidence refs); lease fields (branch, worktree, run id, expiry) are
  caller-supplied configuration. Generated packets are checked by
  ``scripts/validate_packet.validate_packet`` in tests — validator-clean
  by construction.
- **Persistence** — ``engineering_tickets`` (migration 0062) are
  org-scoped, idempotent per (org_id, ticket_id) with slug ids, linked
  to one proposal that must exist (404) and be ACCEPTED or
  IMPLEMENTED (409 ``proposal not ready`` — PROPOSED/REJECTED
  proposals never generate work). The rendered markdown is stored on
  the row. Status lifecycle (pure ``ticket_transition_allowed``):
  GENERATED -> EXPORTED -> IMPLEMENTED; same-state no-op; anything
  else 409. Audited ``engineering_tickets.generated`` and
  ``engineering_tickets.status_changed``.
- **Export** — GET /engineering-tickets/{ticket_id}/export returns the
  packet as ``text/markdown`` for download; the row records EXPORTED
  via the transition route.

Routes (CLIP-033 pattern): POST/GET /engineering-tickets, GET
/engineering-tickets/{ticket_id}, GET .../export,
POST .../transition. No LLM, no network — the generator is a
deterministic renderer over stored proposal data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.improvement import (
    ImprovementProposalRecord,
    ProposalEvidenceRecord,
    ProposalStatus,
)
from clipos_api.repository import Base

MAX_PACKET_CHARS = 32_000


class TicketStatus(str, Enum):
    """Engineering ticket lifecycle."""

    GENERATED = "GENERATED"
    EXPORTED = "EXPORTED"
    IMPLEMENTED = "IMPLEMENTED"


TICKET_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.GENERATED: frozenset({TicketStatus.EXPORTED}),
    TicketStatus.EXPORTED: frozenset({TicketStatus.IMPLEMENTED}),
    TicketStatus.IMPLEMENTED: frozenset(),
}


def ticket_transition_allowed(current: TicketStatus, target: TicketStatus) -> bool:
    """Pure: legal ticket move (same-state counts as no-op)."""
    if current is target:
        return True
    return target in TICKET_TRANSITIONS[current]


def generate_packet_markdown(
    *,
    ticket_id: str,
    orchestrator_run_id: str,
    branch: str,
    worktree: str,
    lease_expires_at: str,
    objective: str,
    weakness_ref: str,
    evidence_refs: list[str],
    acceptance: str | None = None,
    required_reading: str | None = None,
    allowed_scope: str | None = None,
    required_gates: str | None = None,
) -> str:
    """Pure renderer: build a validator-clean Codex task packet.

    Every required heading exists and every section carries content so
    ``scripts/validate_packet.validate_packet`` accepts the result.
    """
    acceptance_block = acceptance or (
        f"- Implements the accepted improvement for: {weakness_ref}\n"
        "- All behavior covered by automated tests.\n"
        "- No provider/model identifier hard-coded where a registry alias is required.\n"
        "- External side effects idempotent and failure-safe.\n"
        "- Documentation and BUILD_STATE evidence updated; CI green."
    )
    evidence_block = (
        "\n".join(f"- {ref}" for ref in evidence_refs)
        if evidence_refs
        else "- (no evidence links recorded)"
    )
    reading_block = required_reading or (
        "- AGENTS.md\n"
        "- .agent/PLANS.md\n"
        "- 01_PRODUCT_REQUIREMENTS.md (journey: self-improve safely)\n"
        "- 08_INTEGRATIONS.md"
    )
    scope_block = allowed_scope or (
        "- paths/domains: as bounded by the accepted proposal\n"
        "- migrations allowed: yes (CREATE-only, next free revision)\n"
        "- external network actions allowed: no"
    )
    gates_block = required_gates or (
        "- ruff format/check services/api\n"
        "- mypy services/api/clipos_api\n"
        "- pytest services/api/tests (SQLite + CI-env)\n"
        "- alembic upgrade head (PG) when migrations added\n"
        "- secret_scan.sh, architecture_guard.py, state_audit.py verify"
    )
    packet = f"""# Codex Task Packet

## Lease
- ticket_id: {ticket_id}
- orchestrator_run_id: {orchestrator_run_id}
- branch: {branch}
- worktree: {worktree}
- lease_expires_at: {lease_expires_at}

## Objective
{objective}

## Acceptance criteria
{acceptance_block}

## Required reading
{reading_block}

## Allowed scope
{scope_block}

## Forbidden actions
- do not merge to main
- do not select unrelated tickets
- do not expose secrets
- do not change human-gate policy
- do not modify another active worktree

## Required gates
{gates_block}

## Completion output
Produce a report conforming to `schemas/codex_completion_report.schema.json` and a concise human-readable summary. Evidence links for this packet: {evidence_block}
"""
    if len(packet) > MAX_PACKET_CHARS:
        raise ValueError(f"generated packet too large: {len(packet)} chars")
    return packet


class EngineeringTicket(BaseModel):
    """One generated engineering ticket (packet + lifecycle)."""

    model_config = ConfigDict(frozen=True)

    ticket_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    status: TicketStatus = TicketStatus.GENERATED
    orchestrator_run_id: str = Field(min_length=1, max_length=128)
    branch: str = Field(min_length=1, max_length=255)
    worktree: str = Field(min_length=1, max_length=512)
    lease_expires_at: str = Field(default="", max_length=64)
    packet_markdown: str = Field(min_length=1, max_length=MAX_PACKET_CHARS)
    created_at: datetime | None = None


class GenerateTicketRequest(BaseModel):
    """Request body for generating one engineering ticket."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    proposal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    orchestrator_run_id: str = Field(min_length=1, max_length=128)
    branch: str = Field(min_length=1, max_length=255)
    worktree: str = Field(min_length=1, max_length=512)
    lease_expires_at: str = Field(default="", max_length=64)


class TicketTransitionRequest(BaseModel):
    """Request body for one ticket lifecycle transition."""

    model_config = ConfigDict(frozen=True)

    status: TicketStatus
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)


class EngineeringTicketRecord(Base):
    """SQL row for one engineering ticket."""

    __tablename__ = "engineering_tickets"
    __table_args__ = (
        sa.Index(
            "uq_engineering_tickets_org_ticket",
            "org_id",
            "ticket_id",
            unique=True,
        ),
        sa.Index("ix_engineering_tickets_org_status", "org_id", "status"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    proposal_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    orchestrator_run_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    branch: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    worktree: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    lease_expires_at: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="")
    packet_markdown: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _ticket_from_row(row: EngineeringTicketRecord) -> EngineeringTicket:
    return EngineeringTicket(
        ticket_id=row.ticket_id,
        org_id=row.org_id,
        proposal_id=row.proposal_id,
        status=TicketStatus(row.status),
        orchestrator_run_id=row.orchestrator_run_id,
        branch=row.branch,
        worktree=row.worktree,
        lease_expires_at=row.lease_expires_at,
        packet_markdown=row.packet_markdown,
        created_at=_aware(row.created_at),
    )


class EngineeringTicketStore:
    """Typed persistence for generated engineering tickets."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> EngineeringTicketStore:
        return cls(
            sessionmaker(bind=engine),
        )

    def generate(self, request: GenerateTicketRequest) -> EngineeringTicket:
        """Render a packet from an accepted proposal and persist it.

        Idempotent per (org, ticket_id); proposal must exist (404) and
        be ACCEPTED or IMPLEMENTED (409); changed inputs conflict (409).
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            proposal = session.scalars(
                sa.select(ImprovementProposalRecord).where(
                    ImprovementProposalRecord.org_id == request.org_id,
                    ImprovementProposalRecord.proposal_id == request.proposal_id,
                )
            ).first()
            if proposal is None:
                raise ValueError(f"improvement proposal not found: {request.proposal_id}")
            if ProposalStatus(proposal.status) not in (
                ProposalStatus.ACCEPTED,
                ProposalStatus.IMPLEMENTED,
            ):
                raise ValueError(
                    f"proposal not ready for engineering ticket: {request.proposal_id}"
                )
            existing = session.scalars(
                sa.select(EngineeringTicketRecord).where(
                    EngineeringTicketRecord.org_id == request.org_id,
                    EngineeringTicketRecord.ticket_id == request.ticket_id,
                )
            ).first()
            if existing is not None:
                stored = _ticket_from_row(existing)
                if (
                    stored.proposal_id != request.proposal_id
                    or stored.branch != request.branch
                    or stored.worktree != request.worktree
                    or stored.orchestrator_run_id != request.orchestrator_run_id
                ):
                    raise ValueError(
                        f"engineering ticket already generated with different fields: "
                        f"{request.ticket_id}"
                    )
                return stored
            evidence_refs = [
                row.evidence_ref
                for row in session.scalars(
                    sa.select(ProposalEvidenceRecord).where(
                        ProposalEvidenceRecord.org_id == request.org_id,
                        ProposalEvidenceRecord.proposal_id == request.proposal_id,
                    )
                ).all()
            ]
            objective = (
                f"Implement the accepted improvement '{proposal.title}' for "
                f"{proposal.weakness_ref}. Summary: {proposal.summary or '(none)'}"
            )
            packet = generate_packet_markdown(
                ticket_id=request.ticket_id,
                orchestrator_run_id=request.orchestrator_run_id,
                branch=request.branch,
                worktree=request.worktree,
                lease_expires_at=request.lease_expires_at,
                objective=objective,
                weakness_ref=proposal.weakness_ref,
                evidence_refs=evidence_refs,
            )
            row = EngineeringTicketRecord(
                ticket_id=request.ticket_id,
                org_id=request.org_id,
                proposal_id=request.proposal_id,
                status=TicketStatus.GENERATED.value,
                orchestrator_run_id=request.orchestrator_run_id,
                branch=request.branch,
                worktree=request.worktree,
                lease_expires_at=request.lease_expires_at,
                packet_markdown=packet,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "engineering_tickets.generated",
                "engineering_tickets",
                f"engineering_ticket:{request.ticket_id}",
                {
                    "org_id": request.org_id,
                    "proposal_id": request.proposal_id,
                    "evidence_count": len(evidence_refs),
                },
            )
            return _ticket_from_row(row)

    def get(self, ticket_id: str, org_id: str) -> EngineeringTicket | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(EngineeringTicketRecord).where(
                    EngineeringTicketRecord.ticket_id == ticket_id,
                    EngineeringTicketRecord.org_id == org_id,
                )
            ).first()
            return _ticket_from_row(row) if row is not None else None

    def list_tickets(self, org_id: str) -> list[EngineeringTicket]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(EngineeringTicketRecord)
                .where(EngineeringTicketRecord.org_id == org_id)
                .order_by(EngineeringTicketRecord.created_at, EngineeringTicketRecord.id)
            ).all()
            return [_ticket_from_row(r) for r in rows]

    def transition(
        self, ticket_id: str, org_id: str, request: TicketTransitionRequest
    ) -> EngineeringTicket:
        """Apply a validated lifecycle transition (404/409)."""
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(EngineeringTicketRecord).where(
                    EngineeringTicketRecord.ticket_id == ticket_id,
                    EngineeringTicketRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"engineering ticket not found: {ticket_id}")
            current = TicketStatus(row.status)
            target = request.status
            if not ticket_transition_allowed(current, target):
                raise ValueError(f"illegal ticket transition: {current.value} -> {target.value}")
            if current is not target:
                row.status = target.value
                session.flush()
                audit.record(
                    "engineering_tickets.status_changed",
                    "engineering_tickets",
                    f"engineering_ticket:{ticket_id}",
                    {
                        "org_id": org_id,
                        "from_status": current.value,
                        "to_status": target.value,
                        "actor_ref": request.actor_ref,
                        "reason_ref": request.reason_ref,
                    },
                )
            return _ticket_from_row(row)


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["engineering-tickets"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> EngineeringTicketStore:
    return EngineeringTicketStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already" in message or "illegal" in message or "not ready" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post(
    "/engineering-tickets",
    response_model=EngineeringTicket,
    name="generate_engineering_ticket",
)
def generate_engineering_ticket_route(
    request: GenerateTicketRequest,
    store: Annotated[EngineeringTicketStore, Depends(_store)],
) -> EngineeringTicket:
    """Generate a Codex task packet from an accepted proposal."""
    try:
        return store.generate(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/engineering-tickets",
    response_model=list[EngineeringTicket],
    name="list_engineering_tickets",
)
def list_engineering_tickets_route(
    store: Annotated[EngineeringTicketStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[EngineeringTicket]:
    """List generated engineering tickets for one org."""
    return store.list_tickets(org_id)


@router.get(
    "/engineering-tickets/{ticket_id}",
    response_model=EngineeringTicket,
    name="get_engineering_ticket",
)
def get_engineering_ticket_route(
    ticket_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[EngineeringTicketStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> EngineeringTicket:
    """Get one engineering ticket by id and org."""
    stored = store.get(ticket_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"engineering ticket not found: {ticket_id}")
    return stored


@router.get(
    "/engineering-tickets/{ticket_id}/export",
    response_class=PlainTextResponse,
    name="export_engineering_ticket",
)
def export_engineering_ticket_route(
    ticket_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[EngineeringTicketStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> PlainTextResponse:
    """Download the task packet as text/markdown."""
    stored = store.get(ticket_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"engineering ticket not found: {ticket_id}")
    return PlainTextResponse(
        content=stored.packet_markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{ticket_id}.md"'},
    )


@router.post(
    "/engineering-tickets/{ticket_id}/transition",
    response_model=EngineeringTicket,
    name="transition_engineering_ticket",
)
def transition_engineering_ticket_route(
    ticket_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: TicketTransitionRequest,
    store: Annotated[EngineeringTicketStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> EngineeringTicket:
    """Advance ticket lifecycle (GENERATED -> EXPORTED -> IMPLEMENTED)."""
    try:
        return store.transition(ticket_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc
