"""Sponsorship campaign attribution (CLIP-072).

PRD §I sponsor campaigns — contracted campaigns, deliverables, and
attributed revenue with a pure summary:

- **Campaigns** — `sponsor_campaigns` rows are org-scoped, idempotent
  per (org_id, campaign_id) with slug ids; each carries an opaque
  sponsor_ref (no brand names required), contracted integer
  **contracted_cents** + 3-letter currency, and a date window.
- **Deliverables** — `sponsor_deliverables` link one content_ref (or
  other opaque ref) to a campaign (404 when the campaign is missing),
  start PENDING and move one-shot to DELIVERED (audited); idempotent
  per (org, deliverable_id) with conflict on changed fields.
- **Attribution rows** — `sponsor_attributions` record attributed
  sponsor revenue against a campaign: integer amount_cents + currency
  + opaque revenue_ref, append-only, idempotent per
  (org, attribution_id), campaign existence enforced (404). Audited
  `sponsor_attributions.recorded`.
- **Summary** — `summarize_sponsorship` is pure: delivered/total
  counts, delivery rate (0 without deliverables), attributed revenue
  per currency (never mixed), and when the attributed total exists in
  the contracted currency, the variance against contracted_cents.

Migration 0050 (3 CREATE-only tables). Routes (CLIP-033 pattern):
POST/GET /sponsor-campaigns, GET /sponsor-campaigns/{campaign_id},
POST/GET /sponsor-deliverables, POST
/sponsor-deliverables/{deliverable_id}/transition, POST
/sponsor-campaigns/{campaign_id}/attributions, GET
/sponsor-campaigns/{campaign_id}/summary. No LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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
from clipos_api.revenue_ledger import CURRENCY_PATTERN


class DeliverableStatus(str, Enum):
    """One-shot deliverable lifecycle."""

    PENDING = "PENDING"
    DELIVERED = "DELIVERED"


class SponsorCampaign(BaseModel):
    """One sponsorship campaign."""

    model_config = ConfigDict(frozen=True)

    campaign_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    sponsor_ref: str = Field(min_length=1, max_length=255)
    contracted_cents: int = Field(ge=0, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    starts_on: date
    ends_on: date
    created_at: datetime | None = None


class SponsorDeliverable(BaseModel):
    """One promised deliverable under a campaign."""

    model_config = ConfigDict(frozen=True)

    deliverable_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    campaign_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    deliverable_kind: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    content_ref: str = Field(min_length=1, max_length=512)
    status: DeliverableStatus = DeliverableStatus.PENDING
    created_at: datetime | None = None


class SponsorAttribution(BaseModel):
    """One attributed sponsor-revenue row (append-only)."""

    model_config = ConfigDict(frozen=True)

    attribution_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    campaign_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    amount_cents: int = Field(ge=-1_000_000_000_000, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    revenue_ref: str = Field(min_length=1, max_length=255)
    created_at: datetime | None = None


class SponsorshipSummary(BaseModel):
    """Pure aggregation for one campaign."""

    model_config = ConfigDict(frozen=True)

    campaign_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    deliverable_count: int = Field(ge=0)
    delivered_count: int = Field(ge=0)
    delivery_rate: float = Field(ge=0.0, le=1.0)
    contracted_cents: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    attributed_revenue_cents_by_currency: dict[str, int] = Field(default_factory=dict)
    variance_cents: int | None = None


class RegisterCampaignRequest(BaseModel):
    """Request body for registering one campaign."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    campaign_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    sponsor_ref: str = Field(min_length=1, max_length=255)
    contracted_cents: int = Field(ge=0, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    starts_on: date
    ends_on: date


class RegisterDeliverableRequest(BaseModel):
    """Request body for registering one deliverable."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    deliverable_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    campaign_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    deliverable_kind: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    content_ref: str = Field(min_length=1, max_length=512)


class DeliverableTransitionRequest(BaseModel):
    """Request body for marking a deliverable DELIVERED."""

    model_config = ConfigDict(frozen=True)

    status: DeliverableStatus
    actor_ref: str = Field(min_length=1, max_length=255)


class RecordAttributionRequest(BaseModel):
    """Request body for recording one attribution row."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    attribution_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    campaign_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    amount_cents: int = Field(ge=-1_000_000_000_000, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    revenue_ref: str = Field(min_length=1, max_length=255)


class SponsorCampaignRecord(Base):
    """SQL row for one sponsor campaign."""

    __tablename__ = "sponsor_campaigns"
    __table_args__ = (
        sa.Index(
            "uq_sponsor_campaigns_org_campaign",
            "org_id",
            "campaign_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    sponsor_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    contracted_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    starts_on: Mapped[date] = mapped_column(sa.Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(sa.Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class SponsorDeliverableRecord(Base):
    """SQL row for one deliverable."""

    __tablename__ = "sponsor_deliverables"
    __table_args__ = (
        sa.Index(
            "uq_sponsor_deliverables_org_deliverable",
            "org_id",
            "deliverable_id",
            unique=True,
        ),
        sa.Index("ix_sponsor_deliverables_org_campaign", "org_id", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    deliverable_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    deliverable_kind: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    content_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class SponsorAttributionRecord(Base):
    """SQL row for one attribution (append-only)."""

    __tablename__ = "sponsor_attributions"
    __table_args__ = (
        sa.Index(
            "uq_sponsor_attributions_org_attribution",
            "org_id",
            "attribution_id",
            unique=True,
        ),
        sa.Index("ix_sponsor_attributions_org_campaign", "org_id", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    attribution_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    amount_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    revenue_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _campaign_from_row(row: SponsorCampaignRecord) -> SponsorCampaign:
    return SponsorCampaign(
        campaign_id=row.campaign_id,
        org_id=row.org_id,
        title=row.title,
        sponsor_ref=row.sponsor_ref,
        contracted_cents=row.contracted_cents,
        currency=row.currency,
        starts_on=row.starts_on,
        ends_on=row.ends_on,
        created_at=_aware(row.created_at),
    )


def _deliverable_from_row(row: SponsorDeliverableRecord) -> SponsorDeliverable:
    return SponsorDeliverable(
        deliverable_id=row.deliverable_id,
        org_id=row.org_id,
        campaign_id=row.campaign_id,
        deliverable_kind=row.deliverable_kind,
        content_ref=row.content_ref,
        status=DeliverableStatus(row.status),
        created_at=_aware(row.created_at),
    )


def _attribution_from_row(row: SponsorAttributionRecord) -> SponsorAttribution:
    return SponsorAttribution(
        attribution_id=row.attribution_id,
        org_id=row.org_id,
        campaign_id=row.campaign_id,
        amount_cents=row.amount_cents,
        currency=row.currency,
        revenue_ref=row.revenue_ref,
        created_at=_aware(row.created_at),
    )


def summarize_sponsorship(
    campaign: SponsorCampaign,
    deliverables: list[SponsorDeliverable],
    attributions: list[SponsorAttribution],
) -> SponsorshipSummary:
    """Pure campaign roll-up: delivery progress and attributed revenue."""
    delivered = sum(1 for d in deliverables if d.status is DeliverableStatus.DELIVERED)
    rate = (delivered / len(deliverables)) if deliverables else 0.0
    revenue: dict[str, int] = {}
    for attribution in attributions:
        revenue[attribution.currency] = (
            revenue.get(attribution.currency, 0) + attribution.amount_cents
        )
    variance: int | None = None
    if campaign.currency in revenue:
        variance = revenue[campaign.currency] - campaign.contracted_cents
    return SponsorshipSummary(
        campaign_id=campaign.campaign_id,
        org_id=campaign.org_id,
        deliverable_count=len(deliverables),
        delivered_count=delivered,
        delivery_rate=rate,
        contracted_cents=campaign.contracted_cents,
        currency=campaign.currency,
        attributed_revenue_cents_by_currency=revenue,
        variance_cents=variance,
    )


class SponsorshipStore:
    """Typed persistence for sponsor campaigns, deliverables, attributions."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> SponsorshipStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_campaign(self, request: RegisterCampaignRequest) -> SponsorCampaign:
        """Idempotent per (org, campaign_id); changed fields conflict."""
        now = datetime.now(UTC)
        if request.ends_on < request.starts_on:
            raise ValueError("ends_on must not precede starts_on")
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(SponsorCampaignRecord).where(
                    SponsorCampaignRecord.org_id == request.org_id,
                    SponsorCampaignRecord.campaign_id == request.campaign_id,
                )
            ).first()
            if existing is not None:
                stored = _campaign_from_row(existing)
                if (
                    stored.title != request.title
                    or stored.sponsor_ref != request.sponsor_ref
                    or stored.contracted_cents != request.contracted_cents
                    or stored.currency != request.currency
                    or stored.starts_on != request.starts_on
                    or stored.ends_on != request.ends_on
                ):
                    raise ValueError(
                        f"sponsor campaign already registered with different fields: "
                        f"{request.campaign_id}"
                    )
                return stored
            row = SponsorCampaignRecord(
                campaign_id=request.campaign_id,
                org_id=request.org_id,
                title=request.title,
                sponsor_ref=request.sponsor_ref,
                contracted_cents=request.contracted_cents,
                currency=request.currency,
                starts_on=request.starts_on,
                ends_on=request.ends_on,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "sponsor_campaigns.registered",
                "sponsor_campaigns",
                f"sponsor_campaign:{request.campaign_id}",
                {
                    "org_id": request.org_id,
                    "sponsor_ref": request.sponsor_ref,
                    "contracted_cents": request.contracted_cents,
                    "currency": request.currency,
                },
            )
            return _campaign_from_row(row)

    def get_campaign(self, campaign_id: str, org_id: str) -> SponsorCampaign | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(SponsorCampaignRecord).where(
                    SponsorCampaignRecord.campaign_id == campaign_id,
                    SponsorCampaignRecord.org_id == org_id,
                )
            ).first()
            return _campaign_from_row(row) if row is not None else None

    def list_campaigns(self, org_id: str) -> list[SponsorCampaign]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(SponsorCampaignRecord)
                .where(SponsorCampaignRecord.org_id == org_id)
                .order_by(SponsorCampaignRecord.created_at, SponsorCampaignRecord.id)
            ).all()
            return [_campaign_from_row(r) for r in rows]

    def register_deliverable(self, request: RegisterDeliverableRequest) -> SponsorDeliverable:
        """Idempotent per (org, deliverable_id); campaign must exist (404)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            campaign = session.scalars(
                sa.select(SponsorCampaignRecord).where(
                    SponsorCampaignRecord.org_id == request.org_id,
                    SponsorCampaignRecord.campaign_id == request.campaign_id,
                )
            ).first()
            if campaign is None:
                raise ValueError(f"sponsor campaign not found: {request.campaign_id}")
            existing = session.scalars(
                sa.select(SponsorDeliverableRecord).where(
                    SponsorDeliverableRecord.org_id == request.org_id,
                    SponsorDeliverableRecord.deliverable_id == request.deliverable_id,
                )
            ).first()
            if existing is not None:
                stored = _deliverable_from_row(existing)
                if (
                    stored.campaign_id != request.campaign_id
                    or stored.deliverable_kind != request.deliverable_kind
                    or stored.content_ref != request.content_ref
                ):
                    raise ValueError(
                        f"deliverable already registered with different fields: "
                        f"{request.deliverable_id}"
                    )
                return stored
            row = SponsorDeliverableRecord(
                deliverable_id=request.deliverable_id,
                org_id=request.org_id,
                campaign_id=request.campaign_id,
                deliverable_kind=request.deliverable_kind,
                content_ref=request.content_ref,
                status=DeliverableStatus.PENDING.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "sponsor_deliverables.registered",
                "sponsor_deliverables",
                f"sponsor_deliverable:{request.deliverable_id}",
                {
                    "org_id": request.org_id,
                    "campaign_id": request.campaign_id,
                    "deliverable_kind": request.deliverable_kind,
                },
            )
            return _deliverable_from_row(row)

    def list_deliverables(
        self, org_id: str, campaign_id: str | None = None
    ) -> list[SponsorDeliverable]:
        with self._sessions() as session:
            stmt = sa.select(SponsorDeliverableRecord).where(
                SponsorDeliverableRecord.org_id == org_id
            )
            if campaign_id is not None:
                stmt = stmt.where(SponsorDeliverableRecord.campaign_id == campaign_id)
            rows = session.scalars(
                stmt.order_by(SponsorDeliverableRecord.created_at, SponsorDeliverableRecord.id)
            ).all()
            return [_deliverable_from_row(r) for r in rows]

    def transition_deliverable(
        self,
        deliverable_id: str,
        org_id: str,
        request: DeliverableTransitionRequest,
    ) -> SponsorDeliverable:
        """One-shot PENDING -> DELIVERED (404 unknown, 409 illegal)."""
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(SponsorDeliverableRecord).where(
                    SponsorDeliverableRecord.deliverable_id == deliverable_id,
                    SponsorDeliverableRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"deliverable not found: {deliverable_id}")
            current = DeliverableStatus(row.status)
            target = request.status
            if current is target:
                return _deliverable_from_row(row)
            if not (current is DeliverableStatus.PENDING and target is DeliverableStatus.DELIVERED):
                raise ValueError(
                    f"illegal deliverable transition: {current.value} -> {target.value}"
                )
            row.status = target.value
            session.flush()
            audit.record(
                "sponsor_deliverables.delivered",
                "sponsor_deliverables",
                f"sponsor_deliverable:{deliverable_id}",
                {
                    "org_id": org_id,
                    "actor_ref": request.actor_ref,
                },
            )
            return _deliverable_from_row(row)

    def record_attribution(self, request: RecordAttributionRequest) -> SponsorAttribution:
        """Idempotent per (org, attribution_id); campaign must exist (404)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            campaign = session.scalars(
                sa.select(SponsorCampaignRecord).where(
                    SponsorCampaignRecord.org_id == request.org_id,
                    SponsorCampaignRecord.campaign_id == request.campaign_id,
                )
            ).first()
            if campaign is None:
                raise ValueError(f"sponsor campaign not found: {request.campaign_id}")
            existing = session.scalars(
                sa.select(SponsorAttributionRecord).where(
                    SponsorAttributionRecord.org_id == request.org_id,
                    SponsorAttributionRecord.attribution_id == request.attribution_id,
                )
            ).first()
            if existing is not None:
                stored = _attribution_from_row(existing)
                if (
                    stored.campaign_id != request.campaign_id
                    or stored.amount_cents != request.amount_cents
                    or stored.currency != request.currency
                    or stored.revenue_ref != request.revenue_ref
                ):
                    raise ValueError(
                        f"sponsor attribution already recorded with different fields: "
                        f"{request.attribution_id}"
                    )
                return stored
            row = SponsorAttributionRecord(
                attribution_id=request.attribution_id,
                org_id=request.org_id,
                campaign_id=request.campaign_id,
                amount_cents=request.amount_cents,
                currency=request.currency,
                revenue_ref=request.revenue_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "sponsor_attributions.recorded",
                "sponsor_attributions",
                f"sponsor_attribution:{request.attribution_id}",
                {
                    "org_id": request.org_id,
                    "campaign_id": request.campaign_id,
                    "amount_cents": request.amount_cents,
                    "currency": request.currency,
                },
            )
            return _attribution_from_row(row)

    def summary(self, campaign_id: str, org_id: str) -> SponsorshipSummary:
        """Attribution summary for one campaign (404 when unknown)."""
        campaign = self.get_campaign(campaign_id, org_id)
        if campaign is None:
            raise ValueError(f"sponsor campaign not found: {campaign_id}")
        deliverables = self.list_deliverables(org_id, campaign_id)
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(SponsorAttributionRecord)
                .where(
                    SponsorAttributionRecord.org_id == org_id,
                    SponsorAttributionRecord.campaign_id == campaign_id,
                )
                .order_by(SponsorAttributionRecord.created_at, SponsorAttributionRecord.id)
            ).all()
            attributions = [_attribution_from_row(r) for r in rows]
        return summarize_sponsorship(campaign, deliverables, attributions)


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["sponsorship"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> SponsorshipStore:
    return SponsorshipStore.from_engine(engine)


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
    "/sponsor-campaigns",
    response_model=SponsorCampaign,
    name="register_sponsor_campaign",
)
def register_sponsor_campaign_route(
    request: RegisterCampaignRequest,
    store: Annotated[SponsorshipStore, Depends(_store)],
) -> SponsorCampaign:
    """Register one sponsor campaign (idempotent per org+campaign_id)."""
    try:
        return store.register_campaign(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/sponsor-campaigns/{campaign_id}",
    response_model=SponsorCampaign,
    name="get_sponsor_campaign",
)
def get_sponsor_campaign_route(
    campaign_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[SponsorshipStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> SponsorCampaign:
    """Get one sponsor campaign by id and org."""
    stored = store.get_campaign(campaign_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"sponsor campaign not found: {campaign_id}")
    return stored


@router.get(
    "/sponsor-campaigns",
    response_model=list[SponsorCampaign],
    name="list_sponsor_campaigns",
)
def list_sponsor_campaigns_route(
    store: Annotated[SponsorshipStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[SponsorCampaign]:
    """List sponsor campaigns for one org."""
    return store.list_campaigns(org_id)


@router.post(
    "/sponsor-campaigns/{campaign_id}/summary",
    response_model=SponsorshipSummary,
    name="sponsor_campaign_summary",
)
def sponsor_campaign_summary_route(
    campaign_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[SponsorshipStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> SponsorshipSummary:
    """Attribution summary (delivery progress + revenue per currency)."""
    try:
        return store.summary(campaign_id, org_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/sponsor-deliverables",
    response_model=SponsorDeliverable,
    name="register_sponsor_deliverable",
)
def register_sponsor_deliverable_route(
    request: RegisterDeliverableRequest,
    store: Annotated[SponsorshipStore, Depends(_store)],
) -> SponsorDeliverable:
    """Register one deliverable (campaign must exist; idempotent)."""
    try:
        return store.register_deliverable(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/sponsor-deliverables",
    response_model=list[SponsorDeliverable],
    name="list_sponsor_deliverables",
)
def list_sponsor_deliverables_route(
    store: Annotated[SponsorshipStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    campaign_id: Annotated[str | None, Query(max_length=128)] = None,
) -> list[SponsorDeliverable]:
    """List deliverables for one org, optionally filtered by campaign."""
    return store.list_deliverables(org_id, campaign_id)


@router.post(
    "/sponsor-deliverables/{deliverable_id}/transition",
    response_model=SponsorDeliverable,
    name="transition_sponsor_deliverable",
)
def transition_sponsor_deliverable_route(
    deliverable_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: DeliverableTransitionRequest,
    store: Annotated[SponsorshipStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> SponsorDeliverable:
    """Mark a deliverable DELIVERED (404 unknown, 409 illegal)."""
    try:
        return store.transition_deliverable(deliverable_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/sponsor-campaigns/{campaign_id}/attributions",
    response_model=SponsorAttribution,
    name="record_sponsor_attribution",
)
def record_sponsor_attribution_route(
    campaign_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: RecordAttributionRequest,
    store: Annotated[SponsorshipStore, Depends(_store)],
) -> SponsorAttribution:
    """Record one attribution row (idempotent; campaign must exist)."""
    if request.campaign_id != campaign_id:
        raise HTTPException(
            status_code=422,
            detail="path and body campaign_id must match",
        )
    try:
        return store.record_attribution(request)
    except ValueError as exc:
        raise _map_error(exc) from exc
