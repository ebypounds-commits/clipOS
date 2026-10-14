"""Improvement proposal evidence model (CLIP-082, M20 opener).

PRD journey 5 "self-improve safely" — proposals with linked evidence
and a guarded review lifecycle:

- **Proposals** — `improvement_proposals` rows are org-scoped,
  idempotent per (org_id, proposal_id) with slug ids: a title, an
  opaque weakness_ref describing the recurring weakness, and a
  summary (<= 2000 chars). Lifecycle (pure
  `proposal_transition_allowed`): PROPOSED -> IN_REVIEW -> ACCEPTED
  or REJECTED (one-shot review), ACCEPTED -> IMPLEMENTED,
  IMPLEMENTED -> VERIFIED or ROLLED_BACK; REJECTED / VERIFIED /
  ROLLED_BACK are terminal; same-state is a no-op; anything else 409.
  Audited `improvement_proposals.registered` and
  `improvement_proposals.status_changed` (actor/reason refs).
- **Evidence links** — `proposal_evidence` rows attach append-only
  evidence to a proposal: unique (org_id, evidence_id), proposal
  existence enforced (404), `evidence_ref` (reference only — never
  evidence content) plus a generic kind slug and optional note_ref.
  Idempotent with conflict on changed fields (409); audited
  `proposal_evidence.linked`.

Migration 0060 (CREATE-only). Routes (CLIP-033 pattern): POST/GET
/improvement-proposals, GET /improvement-proposals/{proposal_id},
POST /improvement-proposals/{proposal_id}/transition, POST/GET
/proposal-evidence. No LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base


class ProposalStatus(str, Enum):
    """Improvement proposal lifecycle."""

    PROPOSED = "PROPOSED"
    IN_REVIEW = "IN_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    IMPLEMENTED = "IMPLEMENTED"
    VERIFIED = "VERIFIED"
    ROLLED_BACK = "ROLLED_BACK"


PROPOSAL_TRANSITIONS: dict[ProposalStatus, frozenset[ProposalStatus]] = {
    ProposalStatus.PROPOSED: frozenset({ProposalStatus.IN_REVIEW}),
    ProposalStatus.IN_REVIEW: frozenset({ProposalStatus.ACCEPTED, ProposalStatus.REJECTED}),
    ProposalStatus.ACCEPTED: frozenset({ProposalStatus.IMPLEMENTED}),
    ProposalStatus.IMPLEMENTED: frozenset({ProposalStatus.VERIFIED, ProposalStatus.ROLLED_BACK}),
    ProposalStatus.REJECTED: frozenset(),
    ProposalStatus.VERIFIED: frozenset(),
    ProposalStatus.ROLLED_BACK: frozenset(),
}


def proposal_transition_allowed(current: ProposalStatus, target: ProposalStatus) -> bool:
    """Pure: legal proposal move (same-state counts as no-op)."""
    if current is target:
        return True
    return target in PROPOSAL_TRANSITIONS[current]


class ImprovementProposal(BaseModel):
    """One improvement proposal."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    weakness_ref: str = Field(min_length=1, max_length=512)
    summary: str = Field(default="", max_length=2000)
    status: ProposalStatus = ProposalStatus.PROPOSED
    created_at: datetime | None = None


class ProposalEvidence(BaseModel):
    """One append-only evidence link on a proposal."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    evidence_ref: str = Field(min_length=1, max_length=512)
    evidence_kind: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    note_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = None


class RegisterProposalRequest(BaseModel):
    """Request body for registering one proposal."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    weakness_ref: str = Field(min_length=1, max_length=512)
    summary: str = Field(default="", max_length=2000)


class ProposalTransitionRequest(BaseModel):
    """Request body for one proposal transition."""

    model_config = ConfigDict(frozen=True)

    status: ProposalStatus
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)


class LinkEvidenceRequest(BaseModel):
    """Request body for linking one evidence reference."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    evidence_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    proposal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    evidence_ref: str = Field(min_length=1, max_length=512)
    evidence_kind: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    note_ref: str | None = Field(default=None, max_length=512)


class ImprovementProposalRecord(Base):
    """SQL row for one improvement proposal."""

    __tablename__ = "improvement_proposals"
    __table_args__ = (
        sa.Index(
            "uq_improvement_proposals_org_proposal",
            "org_id",
            "proposal_id",
            unique=True,
        ),
        sa.Index("ix_improvement_proposals_org_status", "org_id", "status"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    weakness_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    summary: Mapped[str] = mapped_column(sa.String(2000), nullable=False, default="")
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ProposalEvidenceRecord(Base):
    """SQL row for one evidence link (append-only)."""

    __tablename__ = "proposal_evidence"
    __table_args__ = (
        sa.Index(
            "uq_proposal_evidence_org_evidence",
            "org_id",
            "evidence_id",
            unique=True,
        ),
        sa.Index("ix_proposal_evidence_org_proposal", "org_id", "proposal_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    evidence_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    proposal_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    evidence_kind: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    note_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _proposal_from_row(row: ImprovementProposalRecord) -> ImprovementProposal:
    return ImprovementProposal(
        proposal_id=row.proposal_id,
        org_id=row.org_id,
        title=row.title,
        weakness_ref=row.weakness_ref,
        summary=row.summary,
        status=ProposalStatus(row.status),
        created_at=_aware(row.created_at),
    )


def _evidence_from_row(row: ProposalEvidenceRecord) -> ProposalEvidence:
    return ProposalEvidence(
        evidence_id=row.evidence_id,
        org_id=row.org_id,
        proposal_id=row.proposal_id,
        evidence_ref=row.evidence_ref,
        evidence_kind=row.evidence_kind,
        note_ref=row.note_ref,
        created_at=_aware(row.created_at),
    )


class ImprovementStore:
    """Typed persistence for proposals and evidence links."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ImprovementStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_proposal(self, request: RegisterProposalRequest) -> ImprovementProposal:
        """Idempotent per (org, proposal_id); changed fields conflict."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(ImprovementProposalRecord).where(
                    ImprovementProposalRecord.org_id == request.org_id,
                    ImprovementProposalRecord.proposal_id == request.proposal_id,
                )
            ).first()
            if existing is not None:
                stored = _proposal_from_row(existing)
                if (
                    stored.title != request.title
                    or stored.weakness_ref != request.weakness_ref
                    or stored.summary != request.summary
                ):
                    raise ValueError(
                        f"improvement proposal already registered with different fields: "
                        f"{request.proposal_id}"
                    )
                return stored
            row = ImprovementProposalRecord(
                proposal_id=request.proposal_id,
                org_id=request.org_id,
                title=request.title,
                weakness_ref=request.weakness_ref,
                summary=request.summary,
                status=ProposalStatus.PROPOSED.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "improvement_proposals.registered",
                "improvement_proposals",
                f"improvement_proposal:{request.proposal_id}",
                {
                    "org_id": request.org_id,
                    "weakness_ref": request.weakness_ref,
                },
            )
            return _proposal_from_row(row)

    def get_proposal(self, proposal_id: str, org_id: str) -> ImprovementProposal | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ImprovementProposalRecord).where(
                    ImprovementProposalRecord.proposal_id == proposal_id,
                    ImprovementProposalRecord.org_id == org_id,
                )
            ).first()
            return _proposal_from_row(row) if row is not None else None

    def list_proposals(
        self, org_id: str, status: ProposalStatus | None = None
    ) -> list[ImprovementProposal]:
        with self._sessions() as session:
            stmt = sa.select(ImprovementProposalRecord).where(
                ImprovementProposalRecord.org_id == org_id
            )
            if status is not None:
                stmt = stmt.where(ImprovementProposalRecord.status == status.value)
            rows = session.scalars(
                stmt.order_by(ImprovementProposalRecord.created_at, ImprovementProposalRecord.id)
            ).all()
            return [_proposal_from_row(r) for r in rows]

    def transition_proposal(
        self, proposal_id: str, org_id: str, request: ProposalTransitionRequest
    ) -> ImprovementProposal:
        """Apply a validated proposal transition (404/409)."""
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(ImprovementProposalRecord).where(
                    ImprovementProposalRecord.proposal_id == proposal_id,
                    ImprovementProposalRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"improvement proposal not found: {proposal_id}")
            current = ProposalStatus(row.status)
            target = request.status
            if not proposal_transition_allowed(current, target):
                raise ValueError(f"illegal proposal transition: {current.value} -> {target.value}")
            if current is not target:
                row.status = target.value
                session.flush()
                audit.record(
                    "improvement_proposals.status_changed",
                    "improvement_proposals",
                    f"improvement_proposal:{proposal_id}",
                    {
                        "org_id": org_id,
                        "from_status": current.value,
                        "to_status": target.value,
                        "actor_ref": request.actor_ref,
                        "reason_ref": request.reason_ref,
                    },
                )
            return _proposal_from_row(row)

    def link_evidence(self, request: LinkEvidenceRequest) -> ProposalEvidence:
        """Link one evidence ref (proposal must exist; idempotent)."""
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
            existing = session.scalars(
                sa.select(ProposalEvidenceRecord).where(
                    ProposalEvidenceRecord.org_id == request.org_id,
                    ProposalEvidenceRecord.evidence_id == request.evidence_id,
                )
            ).first()
            if existing is not None:
                stored = _evidence_from_row(existing)
                if (
                    stored.proposal_id != request.proposal_id
                    or stored.evidence_ref != request.evidence_ref
                    or stored.evidence_kind != request.evidence_kind
                    or stored.note_ref != request.note_ref
                ):
                    raise ValueError(
                        f"proposal evidence already linked with different fields: "
                        f"{request.evidence_id}"
                    )
                return stored
            row = ProposalEvidenceRecord(
                evidence_id=request.evidence_id,
                org_id=request.org_id,
                proposal_id=request.proposal_id,
                evidence_ref=request.evidence_ref,
                evidence_kind=request.evidence_kind,
                note_ref=request.note_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "proposal_evidence.linked",
                "proposal_evidence",
                f"proposal_evidence:{request.evidence_id}",
                {
                    "org_id": request.org_id,
                    "proposal_id": request.proposal_id,
                    "evidence_kind": request.evidence_kind,
                },
            )
            return _evidence_from_row(row)

    def list_evidence(self, org_id: str, proposal_id: str | None = None) -> list[ProposalEvidence]:
        with self._sessions() as session:
            stmt = sa.select(ProposalEvidenceRecord).where(ProposalEvidenceRecord.org_id == org_id)
            if proposal_id is not None:
                stmt = stmt.where(ProposalEvidenceRecord.proposal_id == proposal_id)
            rows = session.scalars(
                stmt.order_by(ProposalEvidenceRecord.created_at, ProposalEvidenceRecord.id)
            ).all()
            return [_evidence_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["improvement"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ImprovementStore:
    return ImprovementStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already" in message or "illegal" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post(
    "/improvement-proposals",
    response_model=ImprovementProposal,
    name="register_improvement_proposal",
)
def register_improvement_proposal_route(
    request: RegisterProposalRequest,
    store: Annotated[ImprovementStore, Depends(_store)],
) -> ImprovementProposal:
    """Register one improvement proposal (idempotent)."""
    try:
        return store.register_proposal(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/improvement-proposals/{proposal_id}",
    response_model=ImprovementProposal,
    name="get_improvement_proposal",
)
def get_improvement_proposal_route(
    proposal_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[ImprovementStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> ImprovementProposal:
    """Get one proposal by id and org."""
    stored = store.get_proposal(proposal_id, org_id)
    if stored is None:
        raise HTTPException(
            status_code=404, detail=f"improvement proposal not found: {proposal_id}"
        )
    return stored


@router.get(
    "/improvement-proposals",
    response_model=list[ImprovementProposal],
    name="list_improvement_proposals",
)
def list_improvement_proposals_route(
    store: Annotated[ImprovementStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    status: Annotated[ProposalStatus | None, Query()] = None,
) -> list[ImprovementProposal]:
    """List proposals for one org, optionally filtered by status."""
    return store.list_proposals(org_id, status)


@router.post(
    "/improvement-proposals/{proposal_id}/transition",
    response_model=ImprovementProposal,
    name="transition_improvement_proposal",
)
def transition_improvement_proposal_route(
    proposal_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: ProposalTransitionRequest,
    store: Annotated[ImprovementStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> ImprovementProposal:
    """Apply a proposal transition (404 unknown, 409 illegal)."""
    try:
        return store.transition_proposal(proposal_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/proposal-evidence",
    response_model=ProposalEvidence,
    name="link_proposal_evidence",
)
def link_proposal_evidence_route(
    request: LinkEvidenceRequest,
    store: Annotated[ImprovementStore, Depends(_store)],
) -> ProposalEvidence:
    """Link one evidence reference to a proposal (refs only)."""
    try:
        return store.link_evidence(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/proposal-evidence",
    response_model=list[ProposalEvidence],
    name="list_proposal_evidence",
)
def list_proposal_evidence_route(
    store: Annotated[ImprovementStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    proposal_id: Annotated[str | None, Query(max_length=128)] = None,
) -> list[ProposalEvidence]:
    """List evidence links for one org, optionally by proposal."""
    return store.list_evidence(org_id, proposal_id)
