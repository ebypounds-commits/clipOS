"""Cost ledger and provider cost ingestion (CLIP-074, M18 opener).

PRD §I cost ledger — provider-neutral cost ingestion alongside the
existing `llm_cost` dispatch ledger (this module records non-dispatch
provider bills: compute, storage, egress):

- **Adapter contract** — `CostAdapter` Protocol (health_check,
  fetch_costs over one period) with `MockCostAdapter` as the
  deterministic CI stand-in (two entries per fetch; external refs and
  amount_cents derive from sha1(source|period|category) — no network,
  no vendor identifier). Real provider adapters register behind the
  Protocol later, selected per source.
- **Sources** — `cost_sources` rows are org-scoped, idempotent per
  (org_id, source_id) with slug ids, a generic slug (never a vendor
  name), and a vault-only account_ref.
- **Ledger** — `cost_entries` are append-only: integer **amount_cents**
  (never floats; positive cost, negative credit), 3-letter currency,
  (period_start, period_end) window, unique (org_id, source_id,
  external_ref) — a re-sync of the same period skips everything already
  recorded. Costs are `CostCategory` USAGE / STORAGE / EGRESS.
- **Sync flow** — `sync_costs` requires the source (404), validates
  the period (422), fetches through the adapter, persists only new
  entries, and audits each insert (`cost_entries.recorded`).

Migration 0052 (CREATE-only). Routes (CLIP-033 pattern): POST/GET
/cost-sources, GET /cost-sources/{source_id}, POST /cost-sync, GET
/cost-entries. No LLM, no network in the mock path. Distinct from
`llm_cost.CostLedger`, which records in-process model dispatch spend.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from hashlib import sha1
from typing import Annotated, Protocol
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
from clipos_api.revenue_ledger import CURRENCY_PATTERN


class CostCategory(str, Enum):
    """Provider cost classes (PRD §I)."""

    USAGE = "USAGE"
    STORAGE = "STORAGE"
    EGRESS = "EGRESS"


class CostHealth(BaseModel):
    """Normalized health probe for one cost adapter."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    capabilities: list[str] = Field(default_factory=list)
    detail: str | None = Field(default=None, max_length=255)


class CostEntryDraft(BaseModel):
    """One ledger row as produced by an adapter fetch."""

    model_config = ConfigDict(frozen=True)

    external_ref: str = Field(min_length=1, max_length=255)
    cost_category: CostCategory
    amount_cents: int = Field(ge=-1_000_000_000_000, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    period_start: date
    period_end: date


class CostEntry(BaseModel):
    """One persisted ledger row."""

    model_config = ConfigDict(frozen=True)

    entry_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    external_ref: str = Field(min_length=1, max_length=255)
    cost_category: CostCategory
    amount_cents: int
    currency: str = Field(min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    period_start: date
    period_end: date
    created_at: datetime | None = None


class CostAdapter(Protocol):
    """Provider-neutral cost backend."""

    def health_check(self, account_ref: str) -> CostHealth: ...

    def fetch_costs(
        self,
        source_ref: str,
        period_start: date,
        period_end: date,
    ) -> list[CostEntryDraft]: ...


class MockCostAdapter:
    """Deterministic stand-in: no network; refs/amounts derive from sha1."""

    def health_check(self, account_ref: str) -> CostHealth:
        return CostHealth(
            ok=True,
            capabilities=["fetch", "health"],
            detail="mock cost adapter",
        )

    def fetch_costs(
        self,
        source_ref: str,
        period_start: date,
        period_end: date,
    ) -> list[CostEntryDraft]:
        window = f"{period_start.isoformat()}|{period_end.isoformat()}"
        drafts: list[CostEntryDraft] = []
        for category in (CostCategory.USAGE, CostCategory.STORAGE):
            basis = sha1(f"{source_ref}|{window}|{category.value}".encode()).hexdigest()
            drafts.append(
                CostEntryDraft(
                    external_ref=f"mock-{basis[:12]}",
                    cost_category=category,
                    amount_cents=int(basis[:8], 16) % 50_000 + 50,
                    currency="USD",
                    period_start=period_start,
                    period_end=period_end,
                )
            )
        return drafts


class CostSource(BaseModel):
    """One registered cost source."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    account_ref: str = Field(min_length=1, max_length=255)
    cost_center: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    created_at: datetime | None = None


class RegisterSourceRequest(BaseModel):
    """Request body for registering one cost source."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    platform: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    account_ref: str = Field(min_length=1, max_length=255)
    cost_center: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)


class SyncCostsRequest(BaseModel):
    """Request body for one cost sync run."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    period_start: date
    period_end: date


class CostSyncResult(BaseModel):
    """Outcome of one cost sync run."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    period_start: date
    period_end: date
    imported: int = Field(ge=0)
    skipped: int = Field(ge=0)
    entries: list[CostEntry] = Field(default_factory=list)


class CostSourceRecord(Base):
    """SQL row for one cost source."""

    __tablename__ = "cost_sources"
    __table_args__ = (
        sa.Index("uq_cost_sources_org_source", "org_id", "source_id", unique=True),
        sa.Index("ix_cost_sources_org_platform", "org_id", "platform"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    account_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    cost_center: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class CostEntryRecord(Base):
    """SQL row for one cost ledger entry (append-only)."""

    __tablename__ = "cost_entries"
    __table_args__ = (
        sa.Index(
            "uq_cost_entries_identity",
            "org_id",
            "source_id",
            "external_ref",
            unique=True,
        ),
        sa.Index("ix_cost_entries_org_source", "org_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    external_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    cost_category: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    amount_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    period_start: Mapped[date] = mapped_column(sa.Date, nullable=False)
    period_end: Mapped[date] = mapped_column(sa.Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _source_from_row(row: CostSourceRecord) -> CostSource:
    return CostSource(
        source_id=row.source_id,
        org_id=row.org_id,
        platform=row.platform,
        account_ref=row.account_ref,
        cost_center=row.cost_center,
        created_at=_aware(row.created_at),
    )


def _entry_from_row(row: CostEntryRecord) -> CostEntry:
    return CostEntry(
        entry_id=row.entry_id,
        org_id=row.org_id,
        source_id=row.source_id,
        external_ref=row.external_ref,
        cost_category=CostCategory(row.cost_category),
        amount_cents=row.amount_cents,
        currency=row.currency,
        period_start=row.period_start,
        period_end=row.period_end,
        created_at=_aware(row.created_at),
    )


class CostLedgerStore:
    """Typed persistence for cost sources and ledger entries."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> CostLedgerStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_source(self, request: RegisterSourceRequest) -> CostSource:
        """Idempotent per (org, source_id); changed fields conflict."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(CostSourceRecord).where(
                    CostSourceRecord.org_id == request.org_id,
                    CostSourceRecord.source_id == request.source_id,
                )
            ).first()
            if existing is not None:
                stored = _source_from_row(existing)
                if (
                    stored.platform != request.platform
                    or stored.account_ref != request.account_ref
                    or stored.cost_center != request.cost_center
                ):
                    raise ValueError(
                        f"cost source already registered with different fields: {request.source_id}"
                    )
                return stored
            row = CostSourceRecord(
                source_id=request.source_id,
                org_id=request.org_id,
                platform=request.platform,
                account_ref=request.account_ref,
                cost_center=request.cost_center,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "cost_sources.registered",
                "cost_sources",
                f"cost_source:{request.source_id}",
                {
                    "org_id": request.org_id,
                    "platform": request.platform,
                    "cost_center": request.cost_center,
                },
            )
            return _source_from_row(row)

    def get_source(self, source_id: str, org_id: str) -> CostSource | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(CostSourceRecord).where(
                    CostSourceRecord.source_id == source_id,
                    CostSourceRecord.org_id == org_id,
                )
            ).first()
            return _source_from_row(row) if row is not None else None

    def list_sources(self, org_id: str) -> list[CostSource]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(CostSourceRecord)
                .where(CostSourceRecord.org_id == org_id)
                .order_by(CostSourceRecord.created_at, CostSourceRecord.id)
            ).all()
            return [_source_from_row(r) for r in rows]

    def list_entries(self, org_id: str, source_id: str | None = None) -> list[CostEntry]:
        with self._sessions() as session:
            stmt = sa.select(CostEntryRecord).where(CostEntryRecord.org_id == org_id)
            if source_id is not None:
                stmt = stmt.where(CostEntryRecord.source_id == source_id)
            rows = session.scalars(
                stmt.order_by(CostEntryRecord.created_at, CostEntryRecord.id)
            ).all()
            return [_entry_from_row(r) for r in rows]

    def sync(self, adapter: CostAdapter, request: SyncCostsRequest) -> CostSyncResult:
        """Fetch through the adapter and persist only new entries.

        Source must exist (404); inverted period rejected (422);
        entries already recorded for (org, source, external_ref) are
        skipped so repeated syncs import nothing new.
        """
        now = datetime.now(UTC)
        if request.period_end < request.period_start:
            raise ValueError("period_end must not precede period_start")
        with self._sessions() as session:
            source = session.scalars(
                sa.select(CostSourceRecord).where(
                    CostSourceRecord.org_id == request.org_id,
                    CostSourceRecord.source_id == request.source_id,
                )
            ).first()
            if source is None:
                raise ValueError(f"cost source not found: {request.source_id}")
            account_ref = source.account_ref
        drafts = adapter.fetch_costs(account_ref, request.period_start, request.period_end)
        imported = 0
        skipped = 0
        entries: list[CostEntry] = []
        for draft in drafts:
            with self._sessions.begin() as session:
                existing = session.scalars(
                    sa.select(CostEntryRecord).where(
                        CostEntryRecord.org_id == request.org_id,
                        CostEntryRecord.source_id == request.source_id,
                        CostEntryRecord.external_ref == draft.external_ref,
                    )
                ).first()
                if existing is not None:
                    skipped += 1
                    entries.append(_entry_from_row(existing))
                    continue
                entry_id = str(uuid4())[:12]
                row = CostEntryRecord(
                    entry_id=entry_id,
                    org_id=request.org_id,
                    source_id=request.source_id,
                    external_ref=draft.external_ref,
                    cost_category=draft.cost_category.value,
                    amount_cents=draft.amount_cents,
                    currency=draft.currency,
                    period_start=draft.period_start,
                    period_end=draft.period_end,
                    created_at=now,
                )
                session.add(row)
                session.flush()
                audit.record(
                    "cost_entries.recorded",
                    "cost_entries",
                    f"cost_entry:{entry_id}",
                    {
                        "org_id": request.org_id,
                        "source_id": request.source_id,
                        "cost_category": draft.cost_category.value,
                        "amount_cents": draft.amount_cents,
                        "currency": draft.currency,
                    },
                )
                imported += 1
                entries.append(_entry_from_row(row))
        return CostSyncResult(
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

router = APIRouter(tags=["costs"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> CostLedgerStore:
    return CostLedgerStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _adapter() -> CostAdapter:
    return MockCostAdapter()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already registered" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post("/cost-sources", response_model=CostSource, name="register_cost_source")
def register_cost_source_route(
    request: RegisterSourceRequest,
    store: Annotated[CostLedgerStore, Depends(_store)],
) -> CostSource:
    """Register one cost source (idempotent per org+source_id)."""
    try:
        return store.register_source(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/cost-sources/{source_id}",
    response_model=CostSource,
    name="get_cost_source",
)
def get_cost_source_route(
    source_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[CostLedgerStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> CostSource:
    """Get one cost source by id and org."""
    stored = store.get_source(source_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"cost source not found: {source_id}")
    return stored


@router.get(
    "/cost-sources",
    response_model=list[CostSource],
    name="list_cost_sources",
)
def list_cost_sources_route(
    store: Annotated[CostLedgerStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[CostSource]:
    """List cost sources for one org."""
    return store.list_sources(org_id)


@router.post("/cost-sync", response_model=CostSyncResult, name="sync_costs")
def sync_costs_route(
    request: SyncCostsRequest,
    store: Annotated[CostLedgerStore, Depends(_store)],
    adapter: Annotated[CostAdapter, Depends(_adapter)],
) -> CostSyncResult:
    """Fetch one period through the adapter and persist new entries."""
    try:
        return store.sync(adapter, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/cost-entries",
    response_model=list[CostEntry],
    name="list_cost_entries",
)
def list_cost_entries_route(
    store: Annotated[CostLedgerStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    source_id: Annotated[str | None, Query(max_length=128)] = None,
) -> list[CostEntry]:
    """List ledger entries for one org, optionally filtered by source."""
    return store.list_entries(org_id, source_id)
