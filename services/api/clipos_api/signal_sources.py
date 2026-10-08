"""Signal sources and observations: typed definitions and persistence.

Implements the first two tables of the "Signals & Opportunity" cluster:
- signal_sources: a registered source of signals (e.g. a feed, an API, a search
  stream). Fields must be typed, org-scoped, and idempotent per natural key.
- signal_observations: append-only observations captured from a source
  (raw payload reference + captured_at, not content blobs inline).
"""

from __future__ import annotations

import re
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

_SOURCE_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class SignalSource(BaseModel):
    """One registered signal source, org-scoped and idempotent per natural key."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    source_type: str = Field(min_length=1, max_length=32)
    canonical_url: str | None = Field(default=None, max_length=512)
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    @field_validator("source_type")
    @classmethod
    def _source_type_must_be_valid(cls, value: str) -> str:
        """Restrict source_type to a constrained text namespace."""
        if not _SOURCE_TYPE_PATTERN.fullmatch(value):
            raise ValueError(f"source_type must be a lowercase identifier, got: {value}")
        return value


class SignalObservation(BaseModel):
    """One append-only observation captured from a signal source.

    Raw payload reference + captured_at only; no content blobs stored inline.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(
        default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128
    )
    source_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    payload_ref: str = Field(min_length=1, max_length=512)
    captured_at: datetime
    created_at: datetime | None = None


class SignalSourceRecord(Base):
    """SQL row for one registered signal source."""

    __tablename__ = "signal_sources"
    __table_args__ = (
        sa.Index("uq_signal_sources_org_source", "org_id", "source_id", unique=True),
        sa.Index("ix_signal_sources_org_created", "org_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default=sa.text("'{}'")
    )
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class SignalObservationRecord(Base):
    """SQL row for one append-only signal observation."""

    __tablename__ = "signal_observations"
    __table_args__ = (
        sa.Index("uq_signal_observations_observation_id", "observation_id", unique=True),
        sa.Index("ix_signal_observations_org_created", "org_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    observation_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    source_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    captured_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    """Normalize values read from backends that strip the timezone (SQLite)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _source_from_row(row: SignalSourceRecord) -> SignalSource:
    return SignalSource(
        source_id=row.source_id,
        org_id=row.org_id,
        source_type=row.source_type,
        canonical_url=row.canonical_url,
        config=row.config or {},
        created_at=_aware(row.created_at),
    )


def _observation_from_row(row: SignalObservationRecord) -> SignalObservation:
    return SignalObservation(
        observation_id=row.observation_id,
        source_id=row.source_id,
        org_id=row.org_id,
        payload_ref=row.payload_ref,
        captured_at=_aware(row.captured_at),
        created_at=_aware(row.created_at),
    )


class SignalSourcesStore:
    """Typed persistence for signal sources and their observations."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> SignalSourcesStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    def register_source(self, source: SignalSource) -> SignalSource:
        """Register one signal source; a matching re-registration is a no-op."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(SignalSourceRecord).where(
                    SignalSourceRecord.source_id == source.source_id,
                    SignalSourceRecord.org_id == source.org_id,
                )
            ).first()
            if existing is not None:
                stored = _source_from_row(existing)
                if (
                    stored.org_id != source.org_id
                    or stored.source_type != source.source_type
                    or stored.canonical_url != source.canonical_url
                    or stored.config != source.config
                ):
                    raise ValueError(
                        "signal source already registered with different fields: "
                        f"{source.source_id}"
                    )
                return stored
            row = SignalSourceRecord(
                source_id=source.source_id,
                org_id=source.org_id,
                source_type=source.source_type,
                canonical_url=source.canonical_url,
                config=source.config,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "signal_sources.source_registered",
                "signal-sources",
                f"signal_source:{source.source_id}",
                {
                    "org_id": source.org_id,
                    "source_type": source.source_type,
                },
            )
            return _source_from_row(row)

    def get_source(self, source_id: str, org_id: str | None = None) -> SignalSource | None:
        """Return one signal source by id (optionally org-scoped), or None."""
        with self._sessions() as session:
            stmt = sa.select(SignalSourceRecord).where(SignalSourceRecord.source_id == source_id)
            if org_id is not None:
                stmt = stmt.where(SignalSourceRecord.org_id == org_id)
            row = session.scalars(stmt).first()
            return _source_from_row(row) if row is not None else None

    def list_sources(self, org_id: str) -> list[SignalSource]:
        """Return org-scoped sources ordered by source_id."""
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(SignalSourceRecord)
                .where(SignalSourceRecord.org_id == org_id)
                .order_by(SignalSourceRecord.source_id)
            ).all()
            return [_source_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Observations (append-only)
    # ------------------------------------------------------------------

    def append_observation(self, observation: SignalObservation) -> SignalObservation:
        """Append one observation; duplicate observation_id is a no-op returning stored."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            source = session.scalars(
                sa.select(SignalSourceRecord).where(
                    SignalSourceRecord.source_id == observation.source_id
                )
            ).first()
            if source is None:
                raise ValueError(f"signal source not found: {observation.source_id}")
            if source.org_id != observation.org_id:
                raise ValueError("signal source belongs to a different organization")
            existing = session.scalars(
                sa.select(SignalObservationRecord).where(
                    SignalObservationRecord.observation_id == observation.observation_id
                )
            ).first()
            if existing is not None:
                stored = _observation_from_row(existing)
                if (
                    stored.source_id != observation.source_id
                    or stored.org_id != observation.org_id
                    or stored.payload_ref != observation.payload_ref
                ):
                    raise ValueError(
                        "signal observation already recorded with different fields: "
                        f"{observation.observation_id}"
                    )
                return stored
            row = SignalObservationRecord(
                observation_id=observation.observation_id,
                source_id=observation.source_id,
                org_id=observation.org_id,
                payload_ref=observation.payload_ref,
                captured_at=observation.captured_at,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "signal_sources.observation_appended",
                "signal-sources",
                f"signal_observation:{observation.observation_id}",
                {
                    "org_id": observation.org_id,
                    "source_id": observation.source_id,
                },
            )
            return _observation_from_row(row)

    def list_observations(
        self, org_id: str, source_id: str | None = None
    ) -> list[SignalObservation]:
        """Return org-scoped observations ordered by captured_at then id."""
        with self._sessions() as session:
            stmt = sa.select(SignalObservationRecord).where(
                SignalObservationRecord.org_id == org_id
            )
            if source_id is not None:
                stmt = stmt.where(SignalObservationRecord.source_id == source_id)
            rows = session.scalars(
                stmt.order_by(SignalObservationRecord.captured_at, SignalObservationRecord.id)
            ).all()
            return [_observation_from_row(row) for row in rows]


# ----------------------------------------------------------------------
# Router
# ----------------------------------------------------------------------

router = APIRouter(tags=["signal-sources"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> SignalSourcesStore:
    return SignalSourcesStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


@router.post(
    "/signal-sources",
    response_model=SignalSource,
    name="register_signal_source",
)
def register_signal_source_route(
    request: SignalSource,
    store: Annotated[SignalSourcesStore, Depends(_store)],
) -> SignalSource:
    """Register one signal source (idempotent on matching definition)."""
    try:
        return store.register_source(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


@router.get(
    "/signal-sources/{source_id}",
    response_model=SignalSource,
    name="get_signal_source",
)
def get_signal_source_route(
    source_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[SignalSourcesStore, Depends(_store)],
) -> SignalSource:
    """Return one signal source, or 404."""
    stored = store.get_source(source_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"signal source not found: {source_id}")
    return stored


@router.get(
    "/signal-sources",
    response_model=list[SignalSource],
    name="list_signal_sources",
)
def list_signal_sources_route(
    store: Annotated[SignalSourcesStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[SignalSource]:
    """Return org-scoped signal sources."""
    return store.list_sources(org_id)


@router.post(
    "/signal-sources/{source_id}/observations",
    response_model=SignalObservation,
    name="append_signal_observation",
)
def append_signal_observation_route(
    request: SignalObservation,
    source_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[SignalSourcesStore, Depends(_store)],
) -> SignalObservation:
    """Append one observation to a signal source (append-only, idempotent)."""
    if request.source_id != source_id:
        raise HTTPException(
            status_code=422,
            detail="observation source_id must match the path source_id",
        )
    try:
        return store.append_observation(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


@router.get(
    "/signal-sources/{source_id}/observations",
    response_model=list[SignalObservation],
    name="list_signal_observations",
)
def list_signal_observations_route(
    source_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[SignalSourcesStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[SignalObservation]:
    """Return org-scoped observations for one source, ordered by capture time."""
    return store.list_observations(org_id, source_id)
