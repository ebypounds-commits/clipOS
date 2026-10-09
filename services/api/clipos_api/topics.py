"""Topics, topic aliases, and trend snapshots: typed definitions and persistence.

Implements the three remaining tables of the "Signals & Opportunity" cluster:
- topics: canonical topics that raw signal observations normalize into. Fields
  typed, org-scoped, idempotent per natural key (org_id, topic_id). A topic has
  a display label and arbitrary config metadata.
- topic_aliases: alternative surface forms mapping to a canonical topic
  (alias text -> topic). Idempotent per (org_id, alias); a conflicting
  re-registration (same alias to a different topic) raises ValueError.
- trend_snapshots: point-in-time observations of topic momentum/volume.
  Append-only, refs not blobs, ordered by captured_at.
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

_TOPIC_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")


class Topic(BaseModel):
    """One canonical topic, org-scoped and idempotent per natural key (org_id, topic_id)."""

    model_config = ConfigDict(frozen=True)

    topic_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    display_label: str = Field(min_length=1, max_length=255)
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    @field_validator("topic_id")
    @classmethod
    def _topic_id_must_be_valid(cls, value: str) -> str:
        """Restrict topic_id to a constrained text namespace."""
        if not _TOPIC_ID_PATTERN.fullmatch(value):
            raise ValueError(
                f"topic_id must be a lowercase identifier with [a-z0-9_.-], got: {value}"
            )
        return value


class TopicAlias(BaseModel):
    """One alternative surface form mapping to a canonical topic (alias text -> topic)."""

    model_config = ConfigDict(frozen=True)

    alias: str = Field(min_length=1, max_length=255)
    org_id: str = Field(min_length=1, max_length=64)
    topic_id: str = Field(min_length=1, max_length=128)
    created_at: datetime | None = None

    @field_validator("alias")
    @classmethod
    def _alias_must_be_valid(cls, value: str) -> str:
        """Restrict alias to a constrained text namespace."""
        if not _ALIAS_PATTERN.fullmatch(value):
            raise ValueError(f"alias must be a lowercase identifier with [a-z0-9_.-], got: {value}")
        return value


class TrendSnapshot(BaseModel):
    """One append-only point-in-time momentum/volume reading for a topic."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(
        default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128
    )
    topic_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    metrics: dict[str, float] = Field(default_factory=dict)
    source_ref: str = Field(min_length=1, max_length=512)
    captured_at: datetime
    created_at: datetime | None = None


class TopicRecord(Base):
    """SQL row for one canonical topic."""

    __tablename__ = "topics"
    __table_args__ = (
        sa.Index("uq_topics_org_topic", "org_id", "topic_id", unique=True),
        sa.Index("ix_topics_org_created", "org_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    topic_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    display_label: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default=sa.text("'{}'")
    )
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class TopicAliasRecord(Base):
    """SQL row for one topic alias mapping."""

    __tablename__ = "topic_aliases"
    __table_args__ = (
        sa.Index("uq_topic_aliases_org_alias", "org_id", "alias", unique=True),
        sa.Index("ix_topic_aliases_topic", "topic_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    alias: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    topic_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class TrendSnapshotRecord(Base):
    """SQL row for one append-only trend snapshot."""

    __tablename__ = "trend_snapshots"
    __table_args__ = (
        sa.Index("uq_trend_snapshots_snapshot_id", "snapshot_id", unique=True),
        sa.Index("ix_trend_snapshots_topic_time", "topic_id", "captured_at"),
        sa.Index("ix_trend_snapshots_org_created", "org_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    topic_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default=sa.text("'{}'")
    )
    source_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    captured_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    """Normalize values read from backends that strip the timezone (SQLite)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _topic_from_row(row: TopicRecord) -> Topic:
    return Topic(
        topic_id=row.topic_id,
        org_id=row.org_id,
        display_label=row.display_label,
        config=row.config or {},
        created_at=_aware(row.created_at),
    )


def _alias_from_row(row: TopicAliasRecord) -> TopicAlias:
    return TopicAlias(
        alias=row.alias,
        org_id=row.org_id,
        topic_id=row.topic_id,
        created_at=_aware(row.created_at),
    )


def _snapshot_from_row(row: TrendSnapshotRecord) -> TrendSnapshot:
    return TrendSnapshot(
        snapshot_id=row.snapshot_id,
        topic_id=row.topic_id,
        org_id=row.org_id,
        metrics=row.metrics or {},
        source_ref=row.source_ref,
        captured_at=_aware(row.captured_at),
        created_at=_aware(row.created_at),
    )


# ----------------------------------------------------------------------
# Deterministic normalization / clustering (pure functions, no LLM)
# ----------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_topic_text(raw: str) -> str:
    """Fold a raw surface form to the canonical alias namespace (pure)."""
    folded = raw.strip().casefold()
    folded = _NON_ALNUM.sub("-", folded)
    folded = folded.strip("-")
    return folded


def cluster_aliases(aliases: list[str], threshold: float = 0.85) -> list[list[str]]:
    """Group normalized alias strings by token-set Jaccard similarity (pure).

    Deterministic: iterates in sorted order, assigns each alias to the first
    cluster whose representative it meets or exceeds ``threshold``.
    """
    counts: dict[str, int] = {}
    for a in aliases:
        n = normalize_topic_text(a)
        if n:
            counts[n] = counts.get(n, 0) + 1
    clusters: list[list[str]] = []

    def tokens(s: str) -> set[str]:
        return set(s.split("-")) - {""}

    for item in sorted(counts):
        placed = False
        for cluster in clusters:
            rep = cluster[0]
            t1, t2 = tokens(item), tokens(rep)
            if t1 and t2:
                jac = len(t1 & t2) / len(t1 | t2)
                if jac >= threshold:
                    cluster.extend([item] * counts[item])
                    placed = True
                    break
        if not placed:
            clusters.append([item] * counts[item])
    return clusters


class TopicStore:
    """Typed persistence for topics, aliases, and trend snapshots."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> TopicStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------

    def register_topic(self, topic: Topic) -> Topic:
        """Register one canonical topic; a matching re-registration is a no-op."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(TopicRecord).where(
                    TopicRecord.topic_id == topic.topic_id,
                    TopicRecord.org_id == topic.org_id,
                )
            ).first()
            if existing is not None:
                stored = _topic_from_row(existing)
                if stored.display_label != topic.display_label or stored.config != topic.config:
                    raise ValueError(
                        f"topic already registered with different fields: {topic.topic_id}"
                    )
                return stored
            row = TopicRecord(
                topic_id=topic.topic_id,
                org_id=topic.org_id,
                display_label=topic.display_label,
                config=topic.config,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "topics.topic_registered",
                "topics",
                f"topic:{topic.topic_id}",
                {"org_id": topic.org_id},
            )
            return _topic_from_row(row)

    def get_topic(self, topic_id: str, org_id: str) -> Topic | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(TopicRecord).where(
                    TopicRecord.topic_id == topic_id,
                    TopicRecord.org_id == org_id,
                )
            ).first()
            return _topic_from_row(row) if row is not None else None

    def list_topics(self, org_id: str) -> list[Topic]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(TopicRecord)
                .where(TopicRecord.org_id == org_id)
                .order_by(TopicRecord.topic_id)
            ).all()
            return [_topic_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Aliases
    # ------------------------------------------------------------------

    def register_alias(self, alias: TopicAlias) -> TopicAlias:
        """Map an alias to a canonical topic; conflicting re-registration raises."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            topic = session.scalars(
                sa.select(TopicRecord).where(
                    TopicRecord.topic_id == alias.topic_id,
                    TopicRecord.org_id == alias.org_id,
                )
            ).first()
            if topic is None:
                raise ValueError(f"topic not found: {alias.topic_id}")
            existing = session.scalars(
                sa.select(TopicAliasRecord).where(
                    TopicAliasRecord.alias == alias.alias,
                    TopicAliasRecord.org_id == alias.org_id,
                )
            ).first()
            if existing is not None:
                stored = _alias_from_row(existing)
                if stored.topic_id != alias.topic_id:
                    raise ValueError(
                        f"alias already registered with different fields: {alias.alias}"
                    )
                return stored
            row = TopicAliasRecord(
                org_id=alias.org_id,
                alias=alias.alias,
                topic_id=alias.topic_id,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "topics.alias_registered",
                "topics",
                f"topic_alias:{alias.alias}",
                {"org_id": alias.org_id, "topic_id": alias.topic_id},
            )
            return _alias_from_row(row)

    def resolve_alias(self, alias_text: str, org_id: str) -> Topic | None:
        """Normalize and resolve a raw surface form to its canonical topic."""
        normalized = normalize_topic_text(alias_text)
        with self._sessions() as session:
            row = session.scalars(
                sa.select(TopicAliasRecord).where(
                    TopicAliasRecord.alias == normalized,
                    TopicAliasRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                return None
            topic_row = session.scalars(
                sa.select(TopicRecord).where(
                    TopicRecord.topic_id == row.topic_id,
                    TopicRecord.org_id == org_id,
                )
            ).first()
            return _topic_from_row(topic_row) if topic_row is not None else None

    def list_aliases(self, org_id: str) -> list[TopicAlias]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(TopicAliasRecord)
                .where(TopicAliasRecord.org_id == org_id)
                .order_by(TopicAliasRecord.alias)
            ).all()
            return [_alias_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Trend snapshots (append-only)
    # ------------------------------------------------------------------

    def append_snapshot(self, snapshot: TrendSnapshot) -> TrendSnapshot:
        """Append one trend snapshot; duplicate id no-op, conflicts raise."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            topic = session.scalars(
                sa.select(TopicRecord).where(
                    TopicRecord.topic_id == snapshot.topic_id,
                    TopicRecord.org_id == snapshot.org_id,
                )
            ).first()
            if topic is None:
                raise ValueError(f"topic not found: {snapshot.topic_id}")
            existing = session.scalars(
                sa.select(TrendSnapshotRecord).where(
                    TrendSnapshotRecord.snapshot_id == snapshot.snapshot_id
                )
            ).first()
            if existing is not None:
                stored = _snapshot_from_row(existing)
                if stored.topic_id != snapshot.topic_id or stored.metrics != snapshot.metrics:
                    raise ValueError(
                        "trend snapshot already recorded with different fields: "
                        f"{snapshot.snapshot_id}"
                    )
                return stored
            row = TrendSnapshotRecord(
                snapshot_id=snapshot.snapshot_id,
                topic_id=snapshot.topic_id,
                org_id=snapshot.org_id,
                metrics=snapshot.metrics,
                source_ref=snapshot.source_ref,
                captured_at=snapshot.captured_at,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "topics.snapshot_appended",
                "topics",
                f"trend_snapshot:{snapshot.snapshot_id}",
                {"org_id": snapshot.org_id, "topic_id": snapshot.topic_id},
            )
            return _snapshot_from_row(row)

    def list_snapshots(self, org_id: str, topic_id: str | None = None) -> list[TrendSnapshot]:
        with self._sessions() as session:
            stmt = sa.select(TrendSnapshotRecord).where(TrendSnapshotRecord.org_id == org_id)
            if topic_id is not None:
                stmt = stmt.where(TrendSnapshotRecord.topic_id == topic_id)
            rows = session.scalars(
                stmt.order_by(TrendSnapshotRecord.captured_at, TrendSnapshotRecord.id)
            ).all()
            return [_snapshot_from_row(row) for row in rows]


# ----------------------------------------------------------------------
# Router — Depends providers defined BEFORE the route decorators (CLIP-033 rule)
# ----------------------------------------------------------------------

router = APIRouter(tags=["topics"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> TopicStore:
    return TopicStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


@router.post(
    "/topics",
    response_model=Topic,
    name="register_topic",
)
def register_topic_route(
    request: Topic,
    store: Annotated[TopicStore, Depends(_store)],
) -> Topic:
    """Register one canonical topic (idempotent on matching definition)."""
    try:
        return store.register_topic(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


@router.get(
    "/topics/{topic_id}",
    response_model=Topic,
    name="get_topic",
)
def get_topic_route(
    topic_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[TopicStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Topic:
    stored = store.get_topic(topic_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"topic not found: {topic_id}")
    return stored


@router.get(
    "/topics",
    response_model=list[Topic],
    name="list_topics",
)
def list_topics_route(
    store: Annotated[TopicStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[Topic]:
    return store.list_topics(org_id)


@router.post(
    "/topics/{topic_id}/aliases",
    response_model=TopicAlias,
    name="register_topic_alias",
)
def register_topic_alias_route(
    request: TopicAlias,
    topic_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[TopicStore, Depends(_store)],
) -> TopicAlias:
    if request.topic_id != topic_id:
        raise HTTPException(status_code=422, detail="alias topic_id must match the path topic_id")
    try:
        return store.register_alias(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


@router.get(
    "/topics/{topic_id}/aliases",
    response_model=list[TopicAlias],
    name="list_topic_aliases",
)
def list_topic_aliases_route(
    topic_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[TopicStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[TopicAlias]:
    rows = store.list_aliases(org_id)
    return [r for r in rows if r.topic_id == topic_id]


@router.post(
    "/topics/{topic_id}/snapshots",
    response_model=TrendSnapshot,
    name="append_trend_snapshot",
)
def append_trend_snapshot_route(
    request: TrendSnapshot,
    topic_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[TopicStore, Depends(_store)],
) -> TrendSnapshot:
    if request.topic_id != topic_id:
        raise HTTPException(
            status_code=422, detail="snapshot topic_id must match the path topic_id"
        )
    try:
        return store.append_snapshot(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


@router.get(
    "/topics/{topic_id}/snapshots",
    response_model=list[TrendSnapshot],
    name="list_trend_snapshots",
)
def list_trend_snapshots_route(
    topic_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[TopicStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[TrendSnapshot]:
    return store.list_snapshots(org_id, topic_id)
