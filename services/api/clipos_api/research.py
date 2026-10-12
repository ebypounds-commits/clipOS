"""Research packets with claims and claim sources (CLIP-046).

The M11 content-planning foundation from `03_DOMAIN_DATA_MODEL.md`:
``research_packets`` group findings for a topic, ``claims`` carry statement
rows with an UNVERIFIED -> VERIFIED/DISPUTED veracity lifecycle, and
``claim_sources`` link each claim to source references (refs only — never
content). Idempotent registration keyed on client-supplied natural ids
(``research_packet_id``, ``claim_id``) mirroring CLIP-016 opportunity
registration; source links are idempotent per ``(claim_id, source_ref)``.
Veracity changes are append-audited with actor/reason references. No LLM,
no network, no provider identifiers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base


class ClaimVeracity(str, Enum):
    """Verification state of one claim."""

    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    DISPUTED = "DISPUTED"


class SourceSupport(str, Enum):
    """How one source relates to one claim."""

    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"


class ResearchPacket(BaseModel):
    """One research packet; natural key = (org_id, research_packet_id)."""

    model_config = ConfigDict(frozen=True)

    research_packet_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    topic_id: str | None = Field(default=None, min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    summary: str | None = Field(default=None, max_length=2048)
    created_at: datetime | None = None


class Claim(BaseModel):
    """One claim inside a research packet; natural key = (org_id, claim_id)."""

    model_config = ConfigDict(frozen=True)

    claim_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    research_packet_id: str = Field(min_length=1, max_length=128)
    statement: str = Field(min_length=1, max_length=2048)
    veracity: ClaimVeracity = ClaimVeracity.UNVERIFIED
    created_at: datetime | None = None


class ClaimSourceRequest(BaseModel):
    """Request body for linking one source to one claim."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    source_ref: str = Field(min_length=1, max_length=512)
    support: SourceSupport = SourceSupport.SUPPORTS
    quote_ref: str | None = Field(default=None, max_length=512)


class ClaimSource(BaseModel):
    """One persisted claim -> source link (reference only, no content)."""

    model_config = ConfigDict(frozen=True)

    claim_source_id: str = Field(min_length=1, max_length=128)
    claim_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    source_ref: str = Field(min_length=1, max_length=512)
    support: SourceSupport
    quote_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = None


class VeracityRequest(BaseModel):
    """Request body for setting a claim's veracity with actor evidence."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    veracity: ClaimVeracity
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)


class ResearchPacketRecord(Base):
    """SQL row for one research packet."""

    __tablename__ = "research_packets"
    __table_args__ = (
        sa.Index(
            "uq_research_packets_org_packet",
            "org_id",
            "research_packet_id",
            unique=True,
        ),
        sa.Index("ix_research_packets_org", "org_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    research_packet_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    topic_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(sa.String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ClaimRecord(Base):
    """SQL row for one claim."""

    __tablename__ = "claims"
    __table_args__ = (
        sa.Index("uq_claims_org_claim", "org_id", "claim_id", unique=True),
        sa.Index("ix_claims_org_packet", "org_id", "research_packet_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    claim_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    research_packet_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    statement: Mapped[str] = mapped_column(sa.String(2048), nullable=False)
    veracity: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, server_default="UNVERIFIED"
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ClaimSourceRecord(Base):
    """SQL row for one claim -> source link."""

    __tablename__ = "claim_sources"
    __table_args__ = (
        sa.Index(
            "uq_claim_sources_claim_ref",
            "claim_id",
            "source_ref",
            unique=True,
        ),
        sa.Index("ix_claim_sources_org", "org_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    claim_source_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    claim_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    support: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    quote_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _packet_from_row(row: ResearchPacketRecord) -> ResearchPacket:
    return ResearchPacket(
        research_packet_id=row.research_packet_id,
        org_id=row.org_id,
        topic_id=row.topic_id,
        title=row.title,
        summary=row.summary,
        created_at=_aware(row.created_at),
    )


def _claim_from_row(row: ClaimRecord) -> Claim:
    return Claim(
        claim_id=row.claim_id,
        org_id=row.org_id,
        research_packet_id=row.research_packet_id,
        statement=row.statement,
        veracity=ClaimVeracity(row.veracity),
        created_at=_aware(row.created_at),
    )


def _source_from_row(row: ClaimSourceRecord) -> ClaimSource:
    return ClaimSource(
        claim_source_id=row.claim_source_id,
        claim_id=row.claim_id,
        org_id=row.org_id,
        source_ref=row.source_ref,
        support=SourceSupport(row.support),
        quote_ref=row.quote_ref,
        created_at=_aware(row.created_at),
    )


class ResearchStore:
    """Typed persistence for research packets, claims, and claim sources."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ResearchStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # ------------------------------------------------------------------
    # Research packets
    # ------------------------------------------------------------------

    def register_packet(self, packet: ResearchPacket) -> ResearchPacket:
        """Idempotent register per (org, research_packet_id).

        Identical re-register returns the stored row; different fields raise.
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(ResearchPacketRecord).where(
                    ResearchPacketRecord.org_id == packet.org_id,
                    ResearchPacketRecord.research_packet_id == packet.research_packet_id,
                )
            ).first()
            if existing is not None:
                stored = _packet_from_row(existing)
                if (
                    stored.topic_id != packet.topic_id
                    or stored.title != packet.title
                    or stored.summary != packet.summary
                ):
                    raise ValueError(
                        f"research packet already registered with different fields: "
                        f"{packet.research_packet_id}"
                    )
                return stored
            row = ResearchPacketRecord(
                research_packet_id=packet.research_packet_id,
                org_id=packet.org_id,
                topic_id=packet.topic_id,
                title=packet.title,
                summary=packet.summary,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "research_packets.registered",
                "research_packets",
                f"research_packet:{packet.research_packet_id}",
                {
                    "org_id": packet.org_id,
                    "topic_id": packet.topic_id,
                    "title": packet.title,
                },
            )
            return _packet_from_row(row)

    def get_packet(self, research_packet_id: str, org_id: str) -> ResearchPacket | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ResearchPacketRecord).where(
                    ResearchPacketRecord.research_packet_id == research_packet_id,
                    ResearchPacketRecord.org_id == org_id,
                )
            ).first()
            return _packet_from_row(row) if row is not None else None

    def list_packets(self, org_id: str) -> list[ResearchPacket]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ResearchPacketRecord)
                .where(ResearchPacketRecord.org_id == org_id)
                .order_by(ResearchPacketRecord.created_at, ResearchPacketRecord.id)
            ).all()
            return [_packet_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Claims
    # ------------------------------------------------------------------

    def register_claim(self, claim: Claim) -> Claim:
        """Idempotent register per (org, claim_id); packet must exist."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            packet = session.scalars(
                sa.select(ResearchPacketRecord).where(
                    ResearchPacketRecord.org_id == claim.org_id,
                    ResearchPacketRecord.research_packet_id == claim.research_packet_id,
                )
            ).first()
            if packet is None:
                raise ValueError(f"research packet not found: {claim.research_packet_id}")
            existing = session.scalars(
                sa.select(ClaimRecord).where(
                    ClaimRecord.org_id == claim.org_id,
                    ClaimRecord.claim_id == claim.claim_id,
                )
            ).first()
            if existing is not None:
                stored = _claim_from_row(existing)
                if (
                    stored.research_packet_id != claim.research_packet_id
                    or stored.statement != claim.statement
                ):
                    raise ValueError(
                        f"claim already registered with different fields: {claim.claim_id}"
                    )
                return stored
            row = ClaimRecord(
                claim_id=claim.claim_id,
                org_id=claim.org_id,
                research_packet_id=claim.research_packet_id,
                statement=claim.statement,
                veracity=ClaimVeracity.UNVERIFIED.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "claims.registered",
                "claims",
                f"claim:{claim.claim_id}",
                {
                    "org_id": claim.org_id,
                    "research_packet_id": claim.research_packet_id,
                },
            )
            return _claim_from_row(row)

    def get_claim(self, claim_id: str, org_id: str) -> Claim | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ClaimRecord).where(
                    ClaimRecord.claim_id == claim_id,
                    ClaimRecord.org_id == org_id,
                )
            ).first()
            return _claim_from_row(row) if row is not None else None

    def list_claims(self, research_packet_id: str, org_id: str) -> list[Claim]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ClaimRecord)
                .where(
                    ClaimRecord.org_id == org_id,
                    ClaimRecord.research_packet_id == research_packet_id,
                )
                .order_by(ClaimRecord.created_at, ClaimRecord.id)
            ).all()
            return [_claim_from_row(row) for row in rows]

    def set_veracity(
        self,
        claim_id: str,
        org_id: str,
        veracity: ClaimVeracity,
        actor_ref: str,
        reason_ref: str | None = None,
    ) -> Claim:
        """Set claim veracity (any transition); same value is a no-op.

        Each real change audits claims.veracity_changed with actor/reason
        references. Missing claim raises ValueError (route maps to 404).
        """
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(ClaimRecord).where(
                    ClaimRecord.claim_id == claim_id,
                    ClaimRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"claim not found: {claim_id}")
            if row.veracity == veracity.value:
                return _claim_from_row(row)
            previous = row.veracity
            row.veracity = veracity.value
            session.flush()
            audit.record(
                "claims.veracity_changed",
                "claims",
                f"claim:{claim_id}",
                {
                    "org_id": org_id,
                    "from_status": previous,
                    "to_status": veracity.value,
                    "actor_ref": actor_ref,
                    "reason_ref": reason_ref,
                },
            )
            return _claim_from_row(row)

    # ------------------------------------------------------------------
    # Claim sources
    # ------------------------------------------------------------------

    def link_source(self, source: ClaimSourceRequest, claim_id: str) -> ClaimSource:
        """Idempotent link per (claim_id, source_ref); claim must exist.

        Identical re-link returns the stored row; a different support or
        quote_ref for the same key raises (conflict).
        """
        now = datetime.now(UTC)
        claim_source_id = _new_id()
        with self._sessions.begin() as session:
            claim = session.scalars(
                sa.select(ClaimRecord).where(
                    ClaimRecord.claim_id == claim_id,
                    ClaimRecord.org_id == source.org_id,
                )
            ).first()
            if claim is None:
                raise ValueError(f"claim not found: {claim_id}")
            existing = session.scalars(
                sa.select(ClaimSourceRecord).where(
                    ClaimSourceRecord.claim_id == claim_id,
                    ClaimSourceRecord.source_ref == source.source_ref,
                )
            ).first()
            if existing is not None:
                stored = _source_from_row(existing)
                if (
                    stored.support != source.support
                    or stored.quote_ref != source.quote_ref
                    or stored.org_id != source.org_id
                ):
                    raise ValueError(
                        f"claim source already linked with different fields: "
                        f"{claim_id} ref={source.source_ref}"
                    )
                return stored
            row = ClaimSourceRecord(
                claim_source_id=claim_source_id,
                claim_id=claim_id,
                org_id=source.org_id,
                source_ref=source.source_ref,
                support=source.support.value,
                quote_ref=source.quote_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "claim_sources.linked",
                "claim_sources",
                f"claim_source:{claim_source_id}",
                {
                    "org_id": source.org_id,
                    "claim_id": claim_id,
                    "support": source.support.value,
                    "source_ref_prefix": source.source_ref[:64],
                },
            )
            return _source_from_row(row)

    def list_sources(self, claim_id: str, org_id: str) -> list[ClaimSource]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ClaimSourceRecord)
                .where(
                    ClaimSourceRecord.org_id == org_id,
                    ClaimSourceRecord.claim_id == claim_id,
                )
                .order_by(ClaimSourceRecord.created_at, ClaimSourceRecord.id)
            ).all()
            return [_source_from_row(row) for row in rows]


def _new_id() -> str:
    return str(uuid4())[:12]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["research"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ResearchStore:
    return ResearchStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        status = 404
    elif "already" in message:
        status = 409
    else:
        status = 422
    return HTTPException(status_code=status, detail=message)


@router.post(
    "/research-packets",
    response_model=ResearchPacket,
    name="register_research_packet",
)
def register_research_packet_route(
    request: ResearchPacket,
    store: Annotated[ResearchStore, Depends(_store)],
) -> ResearchPacket:
    """Register one research packet (idempotent per org + packet id)."""
    try:
        return store.register_packet(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/research-packets/{research_packet_id}",
    response_model=ResearchPacket,
    name="get_research_packet",
)
def get_research_packet_route(
    research_packet_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[ResearchStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> ResearchPacket:
    """Get one research packet by id and org."""
    stored = store.get_packet(research_packet_id, org_id)
    if stored is None:
        raise HTTPException(
            status_code=404, detail=f"research packet not found: {research_packet_id}"
        )
    return stored


@router.get(
    "/research-packets",
    response_model=list[ResearchPacket],
    name="list_research_packets",
)
def list_research_packets_route(
    store: Annotated[ResearchStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[ResearchPacket]:
    """List research packets for one org."""
    return store.list_packets(org_id)


@router.post(
    "/research-packets/{research_packet_id}/claims",
    response_model=Claim,
    name="register_claim",
)
def register_claim_route(
    request: Claim,
    research_packet_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[ResearchStore, Depends(_store)],
) -> Claim:
    """Add one claim to one packet (idempotent per org + claim id)."""
    if request.research_packet_id != research_packet_id:
        raise HTTPException(
            status_code=422,
            detail="claim research_packet_id must match the path research_packet_id",
        )
    try:
        return store.register_claim(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/research-packets/{research_packet_id}/claims",
    response_model=list[Claim],
    name="list_claims",
)
def list_claims_route(
    research_packet_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[ResearchStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[Claim]:
    """List claims for one packet (404 when the packet is unknown)."""
    if store.get_packet(research_packet_id, org_id) is None:
        raise HTTPException(
            status_code=404, detail=f"research packet not found: {research_packet_id}"
        )
    return store.list_claims(research_packet_id, org_id)


@router.post(
    "/claims/{claim_id}/sources",
    response_model=ClaimSource,
    name="link_claim_source",
)
def link_claim_source_route(
    request: ClaimSourceRequest,
    claim_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[ResearchStore, Depends(_store)],
) -> ClaimSource:
    """Link one source reference to one claim (idempotent per claim + ref)."""
    try:
        return store.link_source(request, claim_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/claims/{claim_id}/sources",
    response_model=list[ClaimSource],
    name="list_claim_sources",
)
def list_claim_sources_route(
    claim_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[ResearchStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[ClaimSource]:
    """List sources for one claim (empty when none)."""
    return store.list_sources(claim_id, org_id)


@router.post(
    "/claims/{claim_id}/veracity",
    response_model=Claim,
    name="set_claim_veracity",
)
def set_claim_veracity_route(
    request: VeracityRequest,
    claim_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[ResearchStore, Depends(_store)],
) -> Claim:
    """Set claim veracity with actor/reason evidence (audited change)."""
    try:
        return store.set_veracity(
            claim_id,
            request.org_id,
            request.veracity,
            actor_ref=request.actor_ref,
            reason_ref=request.reason_ref,
        )
    except ValueError as exc:
        raise _map_error(exc) from exc
