"""Affiliate link/click/conversion attribution (CLIP-071).

PRD §I affiliate revenue tracking — links, click events, conversions,
and a pure attribution summary:

- **Links** — `affiliate_links` rows are org-scoped, idempotent per
  (org_id, link_id) with slug ids; each carries opaque refs
  (campaign_ref, destination_ref) and an optional content_ref tying the
  link to the published asset. References only — no PII, no provider
  identifiers.
- **Clicks** — `affiliate_clicks` are append-only, idempotent per
  (org_id, click_id); each names its link (404 when missing) and a
  pseudonymous visitor_ref. Audited `affiliate_clicks.recorded`.
- **Conversions** — `affiliate_conversions` are append-only,
  idempotent per (org_id, conversion_id), money as integer
  **amount_cents** + 3-letter currency (shared with the CLIP-070
  ledger). An optional click_ref attributes the conversion to one
  click: the click must exist and belong to the same link (404
  otherwise). Audited `affiliate_conversions.recorded`.
- **Attribution** — `summarize_attribution` is pure aggregation over
  stored rows: click/conversion counts, conversion rate (0 without
  clicks), and attributed revenue summed **per currency** (never mixed).

Migration 0049 (3 CREATE-only tables). Routes (CLIP-033 pattern):
POST/GET /affiliate-links, GET /affiliate-links/{link_id},
POST/GET /affiliate-clicks, POST/GET /affiliate-conversions, GET
/affiliate-links/{link_id}/attribution. No LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
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


class AffiliateLink(BaseModel):
    """One affiliate link."""

    model_config = ConfigDict(frozen=True)

    link_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    campaign_ref: str = Field(min_length=1, max_length=512)
    destination_ref: str = Field(min_length=1, max_length=512)
    content_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = None


class AffiliateClick(BaseModel):
    """One recorded click event (append-only)."""

    model_config = ConfigDict(frozen=True)

    click_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    link_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    visitor_ref: str = Field(min_length=1, max_length=255)
    referrer_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = None


class AffiliateConversion(BaseModel):
    """One recorded conversion (append-only, integer money)."""

    model_config = ConfigDict(frozen=True)

    conversion_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    link_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    click_ref: str | None = Field(default=None, max_length=128)
    amount_cents: int = Field(ge=-1_000_000_000_000, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    order_ref: str = Field(min_length=1, max_length=255)
    created_at: datetime | None = None


class AttributionSummary(BaseModel):
    """Pure aggregation for one link."""

    model_config = ConfigDict(frozen=True)

    link_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    click_count: int = Field(ge=0)
    conversion_count: int = Field(ge=0)
    conversion_rate: float = Field(ge=0.0, le=1.0)
    revenue_cents_by_currency: dict[str, int] = Field(default_factory=dict)


class RegisterLinkRequest(BaseModel):
    """Request body for registering one affiliate link."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    link_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    campaign_ref: str = Field(min_length=1, max_length=512)
    destination_ref: str = Field(min_length=1, max_length=512)
    content_ref: str | None = Field(default=None, max_length=512)


class RecordClickRequest(BaseModel):
    """Request body for recording one click."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    click_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    link_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    visitor_ref: str = Field(min_length=1, max_length=255)
    referrer_ref: str | None = Field(default=None, max_length=512)


class RecordConversionRequest(BaseModel):
    """Request body for recording one conversion."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    conversion_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    link_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    click_ref: str | None = Field(default=None, max_length=128)
    amount_cents: int = Field(ge=-1_000_000_000_000, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    order_ref: str = Field(min_length=1, max_length=255)


class AffiliateLinkRecord(Base):
    """SQL row for one affiliate link."""

    __tablename__ = "affiliate_links"
    __table_args__ = (sa.Index("uq_affiliate_links_org_link", "org_id", "link_id", unique=True),)

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    link_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    campaign_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    destination_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    content_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class AffiliateClickRecord(Base):
    """SQL row for one click (append-only)."""

    __tablename__ = "affiliate_clicks"
    __table_args__ = (
        sa.Index("uq_affiliate_clicks_org_click", "org_id", "click_id", unique=True),
        sa.Index("ix_affiliate_clicks_org_link", "org_id", "link_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    click_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    link_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    visitor_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    referrer_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class AffiliateConversionRecord(Base):
    """SQL row for one conversion (append-only, integer money)."""

    __tablename__ = "affiliate_conversions"
    __table_args__ = (
        sa.Index(
            "uq_affiliate_conversions_org_conversion",
            "org_id",
            "conversion_id",
            unique=True,
        ),
        sa.Index("ix_affiliate_conversions_org_link", "org_id", "link_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    conversion_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    link_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    click_ref: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    amount_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    order_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _link_from_row(row: AffiliateLinkRecord) -> AffiliateLink:
    return AffiliateLink(
        link_id=row.link_id,
        org_id=row.org_id,
        campaign_ref=row.campaign_ref,
        destination_ref=row.destination_ref,
        content_ref=row.content_ref,
        created_at=_aware(row.created_at),
    )


def _click_from_row(row: AffiliateClickRecord) -> AffiliateClick:
    return AffiliateClick(
        click_id=row.click_id,
        org_id=row.org_id,
        link_id=row.link_id,
        visitor_ref=row.visitor_ref,
        referrer_ref=row.referrer_ref,
        created_at=_aware(row.created_at),
    )


def _conversion_from_row(row: AffiliateConversionRecord) -> AffiliateConversion:
    return AffiliateConversion(
        conversion_id=row.conversion_id,
        org_id=row.org_id,
        link_id=row.link_id,
        click_ref=row.click_ref,
        amount_cents=row.amount_cents,
        currency=row.currency,
        order_ref=row.order_ref,
        created_at=_aware(row.created_at),
    )


def summarize_attribution(
    link_id: str,
    org_id: str,
    clicks: list[AffiliateClick],
    conversions: list[AffiliateConversion],
) -> AttributionSummary:
    """Pure aggregation: counts, rate, and revenue per currency."""
    revenue: dict[str, int] = {}
    for conversion in conversions:
        revenue[conversion.currency] = revenue.get(conversion.currency, 0) + conversion.amount_cents
    rate = (len(conversions) / len(clicks)) if clicks else 0.0
    return AttributionSummary(
        link_id=link_id,
        org_id=org_id,
        click_count=len(clicks),
        conversion_count=len(conversions),
        conversion_rate=rate,
        revenue_cents_by_currency=revenue,
    )


class AffiliateStore:
    """Typed persistence for affiliate links, clicks, conversions."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> AffiliateStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_link(self, request: RegisterLinkRequest) -> AffiliateLink:
        """Idempotent per (org, link_id); changed fields conflict."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(AffiliateLinkRecord).where(
                    AffiliateLinkRecord.org_id == request.org_id,
                    AffiliateLinkRecord.link_id == request.link_id,
                )
            ).first()
            if existing is not None:
                stored = _link_from_row(existing)
                if (
                    stored.campaign_ref != request.campaign_ref
                    or stored.destination_ref != request.destination_ref
                    or stored.content_ref != request.content_ref
                ):
                    raise ValueError(
                        f"affiliate link already registered with different fields: "
                        f"{request.link_id}"
                    )
                return stored
            row = AffiliateLinkRecord(
                link_id=request.link_id,
                org_id=request.org_id,
                campaign_ref=request.campaign_ref,
                destination_ref=request.destination_ref,
                content_ref=request.content_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "affiliate_links.registered",
                "affiliate_links",
                f"affiliate_link:{request.link_id}",
                {
                    "org_id": request.org_id,
                    "campaign_ref": request.campaign_ref,
                },
            )
            return _link_from_row(row)

    def get_link(self, link_id: str, org_id: str) -> AffiliateLink | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(AffiliateLinkRecord).where(
                    AffiliateLinkRecord.link_id == link_id,
                    AffiliateLinkRecord.org_id == org_id,
                )
            ).first()
            return _link_from_row(row) if row is not None else None

    def list_links(self, org_id: str) -> list[AffiliateLink]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(AffiliateLinkRecord)
                .where(AffiliateLinkRecord.org_id == org_id)
                .order_by(AffiliateLinkRecord.created_at, AffiliateLinkRecord.id)
            ).all()
            return [_link_from_row(r) for r in rows]

    def record_click(self, request: RecordClickRequest) -> AffiliateClick:
        """Idempotent per (org, click_id); link must exist (404)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            link = session.scalars(
                sa.select(AffiliateLinkRecord).where(
                    AffiliateLinkRecord.org_id == request.org_id,
                    AffiliateLinkRecord.link_id == request.link_id,
                )
            ).first()
            if link is None:
                raise ValueError(f"affiliate link not found: {request.link_id}")
            existing = session.scalars(
                sa.select(AffiliateClickRecord).where(
                    AffiliateClickRecord.org_id == request.org_id,
                    AffiliateClickRecord.click_id == request.click_id,
                )
            ).first()
            if existing is not None:
                stored = _click_from_row(existing)
                if (
                    stored.link_id != request.link_id
                    or stored.visitor_ref != request.visitor_ref
                    or stored.referrer_ref != request.referrer_ref
                ):
                    raise ValueError(
                        f"affiliate click already recorded with different fields: "
                        f"{request.click_id}"
                    )
                return stored
            row = AffiliateClickRecord(
                click_id=request.click_id,
                org_id=request.org_id,
                link_id=request.link_id,
                visitor_ref=request.visitor_ref,
                referrer_ref=request.referrer_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "affiliate_clicks.recorded",
                "affiliate_clicks",
                f"affiliate_click:{request.click_id}",
                {
                    "org_id": request.org_id,
                    "link_id": request.link_id,
                },
            )
            return _click_from_row(row)

    def list_clicks(self, org_id: str, link_id: str | None = None) -> list[AffiliateClick]:
        with self._sessions() as session:
            stmt = sa.select(AffiliateClickRecord).where(AffiliateClickRecord.org_id == org_id)
            if link_id is not None:
                stmt = stmt.where(AffiliateClickRecord.link_id == link_id)
            rows = session.scalars(
                stmt.order_by(AffiliateClickRecord.created_at, AffiliateClickRecord.id)
            ).all()
            return [_click_from_row(r) for r in rows]

    def record_conversion(self, request: RecordConversionRequest) -> AffiliateConversion:
        """Idempotent per (org, conversion_id); attributed click validated."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            link = session.scalars(
                sa.select(AffiliateLinkRecord).where(
                    AffiliateLinkRecord.org_id == request.org_id,
                    AffiliateLinkRecord.link_id == request.link_id,
                )
            ).first()
            if link is None:
                raise ValueError(f"affiliate link not found: {request.link_id}")
            if request.click_ref is not None:
                click = session.scalars(
                    sa.select(AffiliateClickRecord).where(
                        AffiliateClickRecord.org_id == request.org_id,
                        AffiliateClickRecord.click_id == request.click_ref,
                    )
                ).first()
                if click is None or click.link_id != request.link_id:
                    raise ValueError(f"affiliate click not found: {request.click_ref}")
            existing = session.scalars(
                sa.select(AffiliateConversionRecord).where(
                    AffiliateConversionRecord.org_id == request.org_id,
                    AffiliateConversionRecord.conversion_id == request.conversion_id,
                )
            ).first()
            if existing is not None:
                stored = _conversion_from_row(existing)
                if (
                    stored.link_id != request.link_id
                    or stored.click_ref != request.click_ref
                    or stored.amount_cents != request.amount_cents
                    or stored.currency != request.currency
                    or stored.order_ref != request.order_ref
                ):
                    raise ValueError(
                        f"affiliate conversion already recorded with different fields: "
                        f"{request.conversion_id}"
                    )
                return stored
            row = AffiliateConversionRecord(
                conversion_id=request.conversion_id,
                org_id=request.org_id,
                link_id=request.link_id,
                click_ref=request.click_ref,
                amount_cents=request.amount_cents,
                currency=request.currency,
                order_ref=request.order_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "affiliate_conversions.recorded",
                "affiliate_conversions",
                f"affiliate_conversion:{request.conversion_id}",
                {
                    "org_id": request.org_id,
                    "link_id": request.link_id,
                    "amount_cents": request.amount_cents,
                    "currency": request.currency,
                },
            )
            return _conversion_from_row(row)

    def list_conversions(
        self, org_id: str, link_id: str | None = None
    ) -> list[AffiliateConversion]:
        with self._sessions() as session:
            stmt = sa.select(AffiliateConversionRecord).where(
                AffiliateConversionRecord.org_id == org_id
            )
            if link_id is not None:
                stmt = stmt.where(AffiliateConversionRecord.link_id == link_id)
            rows = session.scalars(
                stmt.order_by(AffiliateConversionRecord.created_at, AffiliateConversionRecord.id)
            ).all()
            return [_conversion_from_row(r) for r in rows]

    def attribution(self, link_id: str, org_id: str) -> AttributionSummary:
        """Attribution summary for one link (404 when the link is unknown)."""
        if self.get_link(link_id, org_id) is None:
            raise ValueError(f"affiliate link not found: {link_id}")
        clicks = self.list_clicks(org_id, link_id)
        conversions = self.list_conversions(org_id, link_id)
        return summarize_attribution(link_id, org_id, clicks, conversions)


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["affiliate"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> AffiliateStore:
    return AffiliateStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post("/affiliate-links", response_model=AffiliateLink, name="register_affiliate_link")
def register_affiliate_link_route(
    request: RegisterLinkRequest,
    store: Annotated[AffiliateStore, Depends(_store)],
) -> AffiliateLink:
    """Register one affiliate link (idempotent per org+link_id)."""
    try:
        return store.register_link(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/affiliate-links/{link_id}",
    response_model=AffiliateLink,
    name="get_affiliate_link",
)
def get_affiliate_link_route(
    link_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[AffiliateStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> AffiliateLink:
    """Get one affiliate link by id and org."""
    stored = store.get_link(link_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"affiliate link not found: {link_id}")
    return stored


@router.get(
    "/affiliate-links",
    response_model=list[AffiliateLink],
    name="list_affiliate_links",
)
def list_affiliate_links_route(
    store: Annotated[AffiliateStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[AffiliateLink]:
    """List affiliate links for one org."""
    return store.list_links(org_id)


@router.get(
    "/affiliate-links/{link_id}/attribution",
    response_model=AttributionSummary,
    name="attribute_affiliate_link",
)
def attribute_affiliate_link_route(
    link_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[AffiliateStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> AttributionSummary:
    """Attribution summary (counts, rate, revenue per currency)."""
    try:
        return store.attribution(link_id, org_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post("/affiliate-clicks", response_model=AffiliateClick, name="record_affiliate_click")
def record_affiliate_click_route(
    request: RecordClickRequest,
    store: Annotated[AffiliateStore, Depends(_store)],
) -> AffiliateClick:
    """Record one click (idempotent; link must exist)."""
    try:
        return store.record_click(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/affiliate-clicks",
    response_model=list[AffiliateClick],
    name="list_affiliate_clicks",
)
def list_affiliate_clicks_route(
    store: Annotated[AffiliateStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    link_id: Annotated[str | None, Query(max_length=128)] = None,
) -> list[AffiliateClick]:
    """List clicks for one org, optionally filtered by link."""
    return store.list_clicks(org_id, link_id)


@router.post(
    "/affiliate-conversions",
    response_model=AffiliateConversion,
    name="record_affiliate_conversion",
)
def record_affiliate_conversion_route(
    request: RecordConversionRequest,
    store: Annotated[AffiliateStore, Depends(_store)],
) -> AffiliateConversion:
    """Record one conversion (idempotent; optional click attribution)."""
    try:
        return store.record_conversion(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/affiliate-conversions",
    response_model=list[AffiliateConversion],
    name="list_affiliate_conversions",
)
def list_affiliate_conversions_route(
    store: Annotated[AffiliateStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    link_id: Annotated[str | None, Query(max_length=128)] = None,
) -> list[AffiliateConversion]:
    """List conversions for one org, optionally filtered by link."""
    return store.list_conversions(org_id, link_id)
