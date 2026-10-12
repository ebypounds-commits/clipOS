"""Content briefs and the deterministic content-plan engine (CLIP-047).

M11 content-planning engine from PRD domain E: ``content_briefs`` ground a
production ask in a research packet (CLIP-046), and ``content_plans`` are
derived from a brief plus the packet's **VERIFIED** claims only — a
fail-closed factuality rule: no verified claims, no plan. Derivation is a
pure function (no LLM, no network); plans are append-versioned per brief
(identical re-derive is a no-op, changed inputs mint the next version).
Payload carries references (claim ids, brief fields), never content blobs.
No provider identifiers anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base
from clipos_api.research import ClaimRecord, ClaimVeracity, ResearchPacketRecord


class ContentBrief(BaseModel):
    """One production brief; natural key = (org_id, brief_id)."""

    model_config = ConfigDict(frozen=True)

    brief_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    research_packet_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    objective: str = Field(min_length=1, max_length=2048)
    platforms: list[str] = Field(default_factory=list, max_length=16)
    created_at: datetime | None = None

    @field_validator("platforms")
    @classmethod
    def _validate_platforms(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not (1 <= len(entry) <= 64):
                raise ValueError("platform entries must be 1..64 characters")
        return value


class PlanRequest(BaseModel):
    """Request body for deriving one content plan."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)


class ContentPlan(BaseModel):
    """One versioned content plan derived from one brief."""

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128)
    brief_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    derived_at: datetime | None = None


def derive_plan_payload(brief: ContentBrief, verified_claims: list[ClaimRecord]) -> dict[str, Any]:
    """Pure deterministic plan payload from a brief and its VERIFIED claims.

    Claim ids are sorted for byte-stable payloads (version comparisons depend
    on it). Callers must pass only verified claims — the engine enforces the
    factuality gate before invoking this function.
    """
    claim_ids = sorted(claim.claim_id for claim in verified_claims)
    return {
        "brief_id": brief.brief_id,
        "research_packet_id": brief.research_packet_id,
        "objective": brief.objective,
        "platforms": list(brief.platforms),
        "claim_ids": claim_ids,
        "claim_count": len(claim_ids),
    }


class ContentBriefRecord(Base):
    """SQL row for one content brief."""

    __tablename__ = "content_briefs"
    __table_args__ = (
        sa.Index("uq_content_briefs_org_brief", "org_id", "brief_id", unique=True),
        sa.Index("ix_content_briefs_org", "org_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    brief_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    research_packet_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    objective: Mapped[str] = mapped_column(sa.String(2048), nullable=False)
    platforms: Mapped[list[Any]] = mapped_column(
        sa.JSON, nullable=False, default=list, server_default=sa.text("'[]'")
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ContentPlanRecord(Base):
    """SQL row for one versioned content plan."""

    __tablename__ = "content_plans"
    __table_args__ = (
        sa.Index(
            "uq_content_plans_org_brief_version",
            "org_id",
            "brief_id",
            "version",
            unique=True,
        ),
        sa.Index("ix_content_plans_org_brief", "org_id", "brief_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    brief_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default=sa.text("'{}'")
    )
    derived_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _brief_from_row(row: ContentBriefRecord) -> ContentBrief:
    return ContentBrief(
        brief_id=row.brief_id,
        org_id=row.org_id,
        research_packet_id=row.research_packet_id,
        title=row.title,
        objective=row.objective,
        platforms=list(row.platforms or []),
        created_at=_aware(row.created_at),
    )


def _plan_from_row(row: ContentPlanRecord) -> ContentPlan:
    return ContentPlan(
        plan_id=row.plan_id,
        brief_id=row.brief_id,
        org_id=row.org_id,
        version=row.version,
        payload=dict(row.payload or {}),
        derived_at=_aware(row.derived_at),
    )


class ContentPlanningStore:
    """Typed persistence for content briefs and versioned plans."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ContentPlanningStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_brief(self, brief: ContentBrief) -> ContentBrief:
        """Idempotent register per (org, brief_id); packet must exist."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            packet = session.scalars(
                sa.select(ResearchPacketRecord).where(
                    ResearchPacketRecord.org_id == brief.org_id,
                    ResearchPacketRecord.research_packet_id == brief.research_packet_id,
                )
            ).first()
            if packet is None:
                raise ValueError(f"research packet not found: {brief.research_packet_id}")
            existing = session.scalars(
                sa.select(ContentBriefRecord).where(
                    ContentBriefRecord.org_id == brief.org_id,
                    ContentBriefRecord.brief_id == brief.brief_id,
                )
            ).first()
            if existing is not None:
                stored = _brief_from_row(existing)
                if (
                    stored.research_packet_id != brief.research_packet_id
                    or stored.title != brief.title
                    or stored.objective != brief.objective
                    or stored.platforms != brief.platforms
                ):
                    raise ValueError(
                        f"content brief already registered with different fields: {brief.brief_id}"
                    )
                return stored
            row = ContentBriefRecord(
                brief_id=brief.brief_id,
                org_id=brief.org_id,
                research_packet_id=brief.research_packet_id,
                title=brief.title,
                objective=brief.objective,
                platforms=list(brief.platforms),
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "content_briefs.registered",
                "content_briefs",
                f"brief:{brief.brief_id}",
                {
                    "org_id": brief.org_id,
                    "research_packet_id": brief.research_packet_id,
                    "title": brief.title,
                },
            )
            return _brief_from_row(row)

    def get_brief(self, brief_id: str, org_id: str) -> ContentBrief | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ContentBriefRecord).where(
                    ContentBriefRecord.brief_id == brief_id,
                    ContentBriefRecord.org_id == org_id,
                )
            ).first()
            return _brief_from_row(row) if row is not None else None

    def list_briefs(self, org_id: str) -> list[ContentBrief]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ContentBriefRecord)
                .where(ContentBriefRecord.org_id == org_id)
                .order_by(ContentBriefRecord.created_at, ContentBriefRecord.id)
            ).all()
            return [_brief_from_row(row) for row in rows]

    def generate_plan(self, brief_id: str, org_id: str) -> ContentPlan:
        """Derive a plan for one brief (factuality-gated, versioned).

        The brief must exist (404). The plan fails closed: the brief's
        research packet must yield at least one VERIFIED claim (422
        otherwise). Identical re-derivation returns the latest version
        untouched; changed inputs append the next version and audit
        content_plans.generated.
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            brief_row = session.scalars(
                sa.select(ContentBriefRecord).where(
                    ContentBriefRecord.brief_id == brief_id,
                    ContentBriefRecord.org_id == org_id,
                )
            ).first()
            if brief_row is None:
                raise ValueError(f"content brief not found: {brief_id}")
            brief = _brief_from_row(brief_row)
            packet = session.scalars(
                sa.select(ResearchPacketRecord).where(
                    ResearchPacketRecord.org_id == org_id,
                    ResearchPacketRecord.research_packet_id == brief.research_packet_id,
                )
            ).first()
            if packet is None:
                raise ValueError(f"research packet not found: {brief.research_packet_id}")
            claims = session.scalars(
                sa.select(ClaimRecord).where(
                    ClaimRecord.org_id == org_id,
                    ClaimRecord.research_packet_id == brief.research_packet_id,
                )
            ).all()
            verified = [claim for claim in claims if claim.veracity == ClaimVeracity.VERIFIED.value]
            if not verified:
                raise ValueError(f"no verified claims available for planning: {brief_id}")
            payload = derive_plan_payload(brief, verified)
            latest = session.scalars(
                sa.select(ContentPlanRecord)
                .where(
                    ContentPlanRecord.org_id == org_id,
                    ContentPlanRecord.brief_id == brief_id,
                )
                .order_by(ContentPlanRecord.version.desc())
            ).first()
            if latest is not None and dict(latest.payload or {}) == payload:
                return _plan_from_row(latest)
            next_version = (latest.version + 1) if latest is not None else 1
            row = ContentPlanRecord(
                plan_id=str(uuid4())[:12],
                org_id=org_id,
                brief_id=brief_id,
                version=next_version,
                payload=payload,
                derived_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "content_plans.generated",
                "content_plans",
                f"plan:{row.plan_id}",
                {
                    "org_id": org_id,
                    "brief_id": brief_id,
                    "version": next_version,
                    "claim_count": payload["claim_count"],
                },
            )
            return _plan_from_row(row)

    def get_latest_plan(self, brief_id: str, org_id: str) -> ContentPlan | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ContentPlanRecord)
                .where(
                    ContentPlanRecord.org_id == org_id,
                    ContentPlanRecord.brief_id == brief_id,
                )
                .order_by(ContentPlanRecord.version.desc())
            ).first()
            return _plan_from_row(row) if row is not None else None


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["content-planning"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ContentPlanningStore:
    return ContentPlanningStore.from_engine(engine)


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
    "/content-briefs",
    response_model=ContentBrief,
    name="register_content_brief",
)
def register_content_brief_route(
    request: ContentBrief,
    store: Annotated[ContentPlanningStore, Depends(_store)],
) -> ContentBrief:
    """Register one brief against a research packet (idempotent)."""
    try:
        return store.register_brief(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/content-briefs/{brief_id}",
    response_model=ContentBrief,
    name="get_content_brief",
)
def get_content_brief_route(
    brief_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[ContentPlanningStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> ContentBrief:
    """Get one brief by id and org."""
    stored = store.get_brief(brief_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"content brief not found: {brief_id}")
    return stored


@router.get(
    "/content-briefs",
    response_model=list[ContentBrief],
    name="list_content_briefs",
)
def list_content_briefs_route(
    store: Annotated[ContentPlanningStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[ContentBrief]:
    """List briefs for one org."""
    return store.list_briefs(org_id)


@router.post(
    "/content-briefs/{brief_id}/plan",
    response_model=ContentPlan,
    name="generate_content_plan",
)
def generate_content_plan_route(
    request: PlanRequest,
    brief_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[ContentPlanningStore, Depends(_store)],
) -> ContentPlan:
    """Derive the next plan version (factuality-gated on VERIFIED claims)."""
    try:
        return store.generate_plan(brief_id, request.org_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/content-briefs/{brief_id}/plan",
    response_model=ContentPlan | None,
    name="get_latest_content_plan",
)
def get_latest_content_plan_route(
    brief_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[ContentPlanningStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> ContentPlan | None:
    """Return the latest plan version for one brief, or null."""
    return store.get_latest_plan(brief_id, org_id)
