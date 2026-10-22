"""Revenue-source/ledger adapters (CLIP-070, M17 opener).

PRD §I monetization foundation — provider-neutral revenue ingestion with
an append-only ledger:

- **Adapter contract** — `RevenueAdapter` Protocol (health_check,
  fetch_revenue over one period) with `MockRevenueAdapter` as the
  deterministic CI stand-in (two entries per fetch; external refs and
  amount_cents derive from sha1(source|period|kind) — no network, no
  vendor identifier, no provider SDK). Real platform/affiliate/sponsor
  adapters register behind this Protocol later, selected per source.
- **Sources** — `revenue_sources` rows are org-scoped, idempotent per
  (org_id, source_id) with slug ids; each carries a platform slug, an
  account_ref (vault references only), and the RevenueKind it reports
  (PLATFORM / AFFILIATE / SPONSOR / PRODUCT / LEAD_GEN).
- **Ledger** — `revenue_entries` is append-only: money is stored as
  integer **amount_cents** (never floats), 3-letter uppercase currency,
  and a (period_start, period_end) window. Idempotent per
  (org_id, source_id, external_ref): a re-sync skips everything already
  recorded (external-side-effect idempotency); changed rows for the
  same external ref conflict is impossible — rows are never updated.
- **Sync flow** — `sync_revenue` requires the source (404), fetches
  through the adapter, persists only new entries, and audits each
  insert (`revenue_entries.recorded`).

Migration 0048 (CREATE-only). Routes (CLIP-033 pattern): POST/GET
/revenue-sources, GET /revenue-sources/{source_id}, POST /revenue-sync,
GET /revenue-entries. No LLM, no network in the mock path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from hashlib import sha1
from typing import Annotated, Literal, Protocol
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base

CURRENCY_PATTERN = r"^[A-Z]{3}$"


class RevenueKind(str, Enum):
    """Revenue classes from PRD §I."""

    PLATFORM = "PLATFORM"
    AFFILIATE = "AFFILIATE"
    SPONSOR = "SPONSOR"
    PRODUCT = "PRODUCT"
    LEAD_GEN = "LEAD_GEN"


class RevenueHealth(BaseModel):
    """Normalized health probe for one revenue adapter."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    capabilities: list[str] = Field(default_factory=list)
    detail: str | None = Field(default=None, max_length=255)


class RevenueEntryDraft(BaseModel):
    """One ledger row as produced by an adapter fetch."""

    model_config = ConfigDict(frozen=True)

    external_ref: str = Field(min_length=1, max_length=255)
    revenue_kind: RevenueKind
    amount_cents: int = Field(ge=-1_000_000_000_000, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    period_start: date
    period_end: date


class RevenueEntry(BaseModel):
    """One persisted ledger row."""

    model_config = ConfigDict(frozen=True)

    entry_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    external_ref: str = Field(min_length=1, max_length=255)
    revenue_kind: RevenueKind
    amount_cents: int
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    period_start: date
    period_end: date
    created_at: datetime | None = None


class RevenueAdapter(Protocol):
    """Provider-neutral revenue backend."""

    def health_check(self, account_ref: str) -> RevenueHealth: ...

    def fetch_revenue(
        self,
        source_ref: str,
        period_start: date,
        period_end: date,
    ) -> list[RevenueEntryDraft]: ...


class MockRevenueAdapter:
    """Deterministic stand-in: no network; refs/amounts derive from sha1."""

    def health_check(self, account_ref: str) -> RevenueHealth:
        return RevenueHealth(
            ok=True,
            capabilities=["fetch", "health"],
            detail="mock revenue adapter",
        )

    def fetch_revenue(
        self,
        source_ref: str,
        period_start: date,
        period_end: date,
    ) -> list[RevenueEntryDraft]:
        window = f"{period_start.isoformat()}|{period_end.isoformat()}"
        drafts: list[RevenueEntryDraft] = []
        for kind in (RevenueKind.PLATFORM, RevenueKind.AFFILIATE):
            basis = sha1(f"{source_ref}|{window}|{kind.value}".encode()).hexdigest()
            drafts.append(
                RevenueEntryDraft(
                    external_ref=f"mock-{basis[:12]}",
                    revenue_kind=kind,
                    amount_cents=int(basis[:8], 16) % 100_000 + 100,
                    currency="USD",
                    period_start=period_start,
                    period_end=period_end,
                )
            )
        return drafts


class RevenueSource(BaseModel):
    """One registered revenue source."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    account_ref: str = Field(min_length=1, max_length=255)
    revenue_kind: RevenueKind
    created_at: datetime | None = None


class RegisterSourceRequest(BaseModel):
    """Request body for registering one revenue source."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    platform: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    account_ref: str = Field(min_length=1, max_length=255)
    revenue_kind: RevenueKind


class SyncRequest(BaseModel):
    """Request body for one revenue sync run."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    period_start: date
    period_end: date


class SyncResult(BaseModel):
    """Outcome of one revenue sync run."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    period_start: date
    period_end: date
    imported: int = Field(ge=0)
    skipped: int = Field(ge=0)
    entries: list[RevenueEntry] = Field(default_factory=list)
    adapter_kind: Literal["mock", "real"] = "real"


class RevenueSourceRecord(Base):
    """SQL row for one revenue source."""

    __tablename__ = "revenue_sources"
    __table_args__ = (
        sa.Index("uq_revenue_sources_org_source", "org_id", "source_id", unique=True),
        sa.Index("ix_revenue_sources_org_platform", "org_id", "platform"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    account_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    revenue_kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class RevenueEntryRecord(Base):
    """SQL row for one ledger entry (append-only)."""

    __tablename__ = "revenue_entries"
    __table_args__ = (
        sa.Index(
            "uq_revenue_entries_identity",
            "org_id",
            "source_id",
            "external_ref",
            unique=True,
        ),
        sa.Index("ix_revenue_entries_org_source", "org_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    external_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    revenue_kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    amount_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    period_start: Mapped[date] = mapped_column(sa.Date, nullable=False)
    period_end: Mapped[date] = mapped_column(sa.Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _source_from_row(row: RevenueSourceRecord) -> RevenueSource:
    return RevenueSource(
        source_id=row.source_id,
        org_id=row.org_id,
        platform=row.platform,
        account_ref=row.account_ref,
        revenue_kind=RevenueKind(row.revenue_kind),
        created_at=_aware(row.created_at),
    )


def _entry_from_row(row: RevenueEntryRecord) -> RevenueEntry:
    return RevenueEntry(
        entry_id=row.entry_id,
        org_id=row.org_id,
        source_id=row.source_id,
        external_ref=row.external_ref,
        revenue_kind=RevenueKind(row.revenue_kind),
        amount_cents=row.amount_cents,
        currency=row.currency,
        period_start=row.period_start,
        period_end=row.period_end,
        created_at=_aware(row.created_at),
    )


class RevenueLedgerStore:
    """Typed persistence for revenue sources and ledger entries."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> RevenueLedgerStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_source(self, request: RegisterSourceRequest) -> RevenueSource:
        """Idempotent per (org, source_id); changed fields conflict."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(RevenueSourceRecord).where(
                    RevenueSourceRecord.org_id == request.org_id,
                    RevenueSourceRecord.source_id == request.source_id,
                )
            ).first()
            if existing is not None:
                stored = _source_from_row(existing)
                if (
                    stored.platform != request.platform
                    or stored.account_ref != request.account_ref
                    or stored.revenue_kind != request.revenue_kind
                ):
                    raise ValueError(
                        f"revenue source already registered with different fields: "
                        f"{request.source_id}"
                    )
                return stored
            row = RevenueSourceRecord(
                source_id=request.source_id,
                org_id=request.org_id,
                platform=request.platform,
                account_ref=request.account_ref,
                revenue_kind=request.revenue_kind.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "revenue_sources.registered",
                "revenue_sources",
                f"revenue_source:{request.source_id}",
                {
                    "org_id": request.org_id,
                    "platform": request.platform,
                    "revenue_kind": request.revenue_kind.value,
                },
            )
            return _source_from_row(row)

    def get_source(self, source_id: str, org_id: str) -> RevenueSource | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(RevenueSourceRecord).where(
                    RevenueSourceRecord.source_id == source_id,
                    RevenueSourceRecord.org_id == org_id,
                )
            ).first()
            return _source_from_row(row) if row is not None else None

    def list_sources(self, org_id: str) -> list[RevenueSource]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(RevenueSourceRecord)
                .where(RevenueSourceRecord.org_id == org_id)
                .order_by(RevenueSourceRecord.created_at, RevenueSourceRecord.id)
            ).all()
            return [_source_from_row(r) for r in rows]

    def list_entries(self, org_id: str, source_id: str | None = None) -> list[RevenueEntry]:
        with self._sessions() as session:
            stmt = sa.select(RevenueEntryRecord).where(RevenueEntryRecord.org_id == org_id)
            if source_id is not None:
                stmt = stmt.where(RevenueEntryRecord.source_id == source_id)
            rows = session.scalars(
                stmt.order_by(RevenueEntryRecord.created_at, RevenueEntryRecord.id)
            ).all()
            return [_entry_from_row(r) for r in rows]

    def sync(
        self,
        adapter: RevenueAdapter,
        request: SyncRequest,
    ) -> SyncResult:
        """Fetch through the adapter and persist only new entries.

        Source must exist (404). Entries already recorded for
        (org, source_id, external_ref) are skipped — a repeated sync of
        the same period imports nothing new.
        """
        now = datetime.now(UTC)
        if request.period_end < request.period_start:
            raise ValueError("period_end must not precede period_start")
        with self._sessions() as session:
            source = session.scalars(
                sa.select(RevenueSourceRecord).where(
                    RevenueSourceRecord.org_id == request.org_id,
                    RevenueSourceRecord.source_id == request.source_id,
                )
            ).first()
            if source is None:
                raise ValueError(f"revenue source not found: {request.source_id}")
            account_ref = source.account_ref
        drafts = adapter.fetch_revenue(account_ref, request.period_start, request.period_end)
        imported = 0
        skipped = 0
        entries: list[RevenueEntry] = []
        for draft in drafts:
            with self._sessions.begin() as session:
                existing = session.scalars(
                    sa.select(RevenueEntryRecord).where(
                        RevenueEntryRecord.org_id == request.org_id,
                        RevenueEntryRecord.source_id == request.source_id,
                        RevenueEntryRecord.external_ref == draft.external_ref,
                    )
                ).first()
                if existing is not None:
                    skipped += 1
                    entries.append(_entry_from_row(existing))
                    continue
                entry_id = str(uuid4())[:12]
                row = RevenueEntryRecord(
                    entry_id=entry_id,
                    org_id=request.org_id,
                    source_id=request.source_id,
                    external_ref=draft.external_ref,
                    revenue_kind=draft.revenue_kind.value,
                    amount_cents=draft.amount_cents,
                    currency=draft.currency,
                    period_start=draft.period_start,
                    period_end=draft.period_end,
                    created_at=now,
                )
                session.add(row)
                session.flush()
                audit.record(
                    "revenue_entries.recorded",
                    "revenue_entries",
                    f"revenue_entry:{entry_id}",
                    {
                        "org_id": request.org_id,
                        "source_id": request.source_id,
                        "revenue_kind": draft.revenue_kind.value,
                        "amount_cents": draft.amount_cents,
                        "currency": draft.currency,
                    },
                )
                imported += 1
                entries.append(_entry_from_row(row))
        return SyncResult(
            source_id=request.source_id,
            period_start=request.period_start,
            period_end=request.period_end,
            imported=imported,
            skipped=skipped,
            entries=entries,
        )


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["revenue"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> RevenueLedgerStore:
    return RevenueLedgerStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _adapter() -> RevenueAdapter:
    return MockRevenueAdapter()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already registered" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post("/revenue-sources", response_model=RevenueSource, name="register_revenue_source")
def register_revenue_source_route(
    request: RegisterSourceRequest,
    store: Annotated[RevenueLedgerStore, Depends(_store)],
) -> RevenueSource:
    """Register one revenue source (idempotent per org+source_id)."""
    try:
        return store.register_source(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/revenue-sources/{source_id}",
    response_model=RevenueSource,
    name="get_revenue_source",
)
def get_revenue_source_route(
    source_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[RevenueLedgerStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> RevenueSource:
    """Get one revenue source by id and org."""
    stored = store.get_source(source_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"revenue source not found: {source_id}")
    return stored


@router.get(
    "/revenue-sources",
    response_model=list[RevenueSource],
    name="list_revenue_sources",
)
def list_revenue_sources_route(
    store: Annotated[RevenueLedgerStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[RevenueSource]:
    """List revenue sources for one org."""
    return store.list_sources(org_id)


@router.post("/revenue-sync", response_model=SyncResult, name="sync_revenue")
def sync_revenue_route(
    request: SyncRequest,
    store: Annotated[RevenueLedgerStore, Depends(_store)],
    adapter: Annotated[RevenueAdapter, Depends(_adapter)],
) -> SyncResult:
    """Fetch one period through the adapter and persist new entries."""
    try:
        return store.sync(adapter, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/revenue-entries",
    response_model=list[RevenueEntry],
    name="list_revenue_entries",
)
def list_revenue_entries_route(
    store: Annotated[RevenueLedgerStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    source_id: Annotated[str | None, Query(max_length=128)] = None,
) -> list[RevenueEntry]:
    """List ledger entries for one org, optionally filtered by source."""
    return store.list_entries(org_id, source_id)
