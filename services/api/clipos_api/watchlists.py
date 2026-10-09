"""Watchlists, dashboard aggregation, and the Trend workflow runner (CLIP-037).

Implements the watchlist slice of the "Signals & Opportunity" cluster:
- watchlists: org-scoped watch entries, idempotent per (org_id, watchlist_id).
- watchlist_summary: pure per-topic latest snapshot vs threshold aggregation
  with a boolean ``breached`` flag.
- watchlist_alerts: append-only alert rows (unique on alert_id only).
- TrendWorkflowRun / TrendWorkflowRunner: deterministic local workflow runner
  (no Temporal import) that a Temporal worker can later call: fetch entries,
  compute summary, persist alerts for breached topics, audit each step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base


class RunStatus(str, Enum):
    """Lifecycle status of one Trend workflow run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class WatchlistEntry(BaseModel):
    """One org-scoped watch entry on a topic with an alert threshold."""

    model_config = ConfigDict(frozen=True)

    watchlist_id: str = Field(
        default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128
    )
    org_id: str = Field(min_length=1, max_length=64)
    topic_id: str = Field(min_length=1, max_length=128)
    threshold: float
    active: bool = True
    created_at: datetime | None = None


class WatchlistSnapshot(BaseModel):
    """Latest observed metric snapshot for one topic (input to the summary)."""

    model_config = ConfigDict(frozen=True)

    topic_id: str = Field(min_length=1, max_length=128)
    metric_name: str = Field(min_length=1, max_length=128)
    metric_value: float
    observed_at: datetime | None = None


class WatchlistSummaryRow(BaseModel):
    """Per-topic watchlist dashboard row: latest metric vs threshold."""

    model_config = ConfigDict(frozen=True)

    watchlist_id: str
    topic_id: str
    metric_name: str | None = None
    metric_value: float | None = None
    threshold: float
    breached: bool


class WatchlistAlert(BaseModel):
    """One append-only alert row emitted by a workflow run."""

    model_config = ConfigDict(frozen=True)

    alert_id: str = Field(default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    topic_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    metric_name: str = Field(min_length=1, max_length=128)
    metric_value: float
    threshold: float
    created_at: datetime | None = None


class TrendWorkflowRun(BaseModel):
    """One deterministic Trend workflow run."""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(default_factory=lambda: str(uuid4())[:12], min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    status: RunStatus = RunStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_class: str | None = Field(default=None, max_length=128)


class WatchlistEntryRecord(Base):
    """SQL row for one watchlist entry."""

    __tablename__ = "watchlists"
    __table_args__ = (
        sa.Index("uq_watchlists_org_watch", "org_id", "watchlist_id", unique=True),
        sa.Index("ix_watchlists_topic", "topic_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    topic_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    threshold: Mapped[float] = mapped_column(sa.Float, nullable=False)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class WatchlistAlertRecord(Base):
    """SQL row for one append-only watchlist alert."""

    __tablename__ = "watchlist_alerts"
    __table_args__ = (
        sa.Index("uq_watchlist_alerts_alert_id", "alert_id", unique=True),
        sa.Index("uq_watchlist_alerts_run_topic", "run_id", "topic_id", unique=True),
        sa.Index("ix_watchlist_alerts_org", "org_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    topic_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    metric_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    metric_value: Mapped[float] = mapped_column(sa.Float, nullable=False)
    threshold: Mapped[float] = mapped_column(sa.Float, nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _entry_from_row(row: WatchlistEntryRecord) -> WatchlistEntry:
    return WatchlistEntry(
        watchlist_id=row.watchlist_id,
        org_id=row.org_id,
        topic_id=row.topic_id,
        threshold=row.threshold,
        active=row.active,
        created_at=_aware(row.created_at),
    )


def _alert_from_row(row: WatchlistAlertRecord) -> WatchlistAlert:
    return WatchlistAlert(
        alert_id=row.alert_id,
        run_id=row.run_id,
        topic_id=row.topic_id,
        org_id=row.org_id,
        metric_name=row.metric_name,
        metric_value=row.metric_value,
        threshold=row.threshold,
        created_at=_aware(row.created_at),
    )


def watchlist_summary(
    entries: list[WatchlistEntry],
    latest_snapshots: dict[str, WatchlistSnapshot],
) -> list[WatchlistSummaryRow]:
    """Pure dashboard aggregation: per-entry latest metric vs threshold.

    Entries are compared against the snapshot for their topic (if any). An
    entry without a snapshot breaches by definition (no data is a signal).
    Output is deterministic: sorted by (org_id, topic_id, watchlist_id).
    """
    rows: list[WatchlistSummaryRow] = []
    for entry in entries:
        snapshot = latest_snapshots.get(entry.topic_id)
        if snapshot is None:
            rows.append(
                WatchlistSummaryRow(
                    watchlist_id=entry.watchlist_id,
                    topic_id=entry.topic_id,
                    metric_name=None,
                    metric_value=None,
                    threshold=entry.threshold,
                    breached=True,
                )
            )
            continue
        breached = snapshot.metric_value >= entry.threshold
        rows.append(
            WatchlistSummaryRow(
                watchlist_id=entry.watchlist_id,
                topic_id=entry.topic_id,
                metric_name=snapshot.metric_name,
                metric_value=snapshot.metric_value,
                threshold=entry.threshold,
                breached=breached,
            )
        )
    return sorted(
        rows,
        key=lambda r: (
            next(e.org_id for e in entries if e.watchlist_id == r.watchlist_id),
            r.topic_id,
            r.watchlist_id,
        ),
    )


class WatchlistStore:
    """Typed persistence for watchlist entries, alerts, and workflow runs."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> WatchlistStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------

    def register_entry(self, entry: WatchlistEntry) -> WatchlistEntry:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(WatchlistEntryRecord).where(
                    WatchlistEntryRecord.watchlist_id == entry.watchlist_id,
                    WatchlistEntryRecord.org_id == entry.org_id,
                )
            ).first()
            if existing is not None:
                stored = _entry_from_row(existing)
                if (
                    stored.topic_id != entry.topic_id
                    or stored.threshold != entry.threshold
                    or stored.active != entry.active
                ):
                    raise ValueError(
                        "watchlist entry already registered with different fields: "
                        f"{entry.watchlist_id}"
                    )
                return stored
            row = WatchlistEntryRecord(
                watchlist_id=entry.watchlist_id,
                org_id=entry.org_id,
                topic_id=entry.topic_id,
                threshold=entry.threshold,
                active=entry.active,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "watchlists.entry_registered",
                "watchlists",
                f"watchlist:{entry.watchlist_id}",
                {"org_id": entry.org_id, "topic_id": entry.topic_id},
            )
            return _entry_from_row(row)

    def list_entries(self, org_id: str, active_only: bool = True) -> list[WatchlistEntry]:
        with self._sessions() as session:
            stmt = sa.select(WatchlistEntryRecord).where(WatchlistEntryRecord.org_id == org_id)
            if active_only:
                stmt = stmt.where(WatchlistEntryRecord.active.is_(True))
            rows = session.scalars(stmt.order_by(WatchlistEntryRecord.watchlist_id)).all()
            return [_entry_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Alerts (append-only)
    # ------------------------------------------------------------------

    def record_alert(self, alert: WatchlistAlert) -> WatchlistAlert:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(WatchlistAlertRecord).where(
                    WatchlistAlertRecord.run_id == alert.run_id,
                    WatchlistAlertRecord.topic_id == alert.topic_id,
                )
            ).first()
            if existing is not None:
                stored = _alert_from_row(existing)
                if (
                    stored.metric_name != alert.metric_name
                    or stored.metric_value != alert.metric_value
                    or stored.threshold != alert.threshold
                ):
                    raise ValueError(
                        "alert already recorded for this run/topic with different fields: "
                        f"{alert.run_id}/{alert.topic_id}"
                    )
                return stored
            row = WatchlistAlertRecord(
                alert_id=alert.alert_id,
                run_id=alert.run_id,
                topic_id=alert.topic_id,
                org_id=alert.org_id,
                metric_name=alert.metric_name,
                metric_value=alert.metric_value,
                threshold=alert.threshold,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "watchlists.alert_recorded",
                "watchlists",
                f"watchlist_alert:{alert.alert_id}",
                {
                    "org_id": alert.org_id,
                    "run_id": alert.run_id,
                    "topic_id": alert.topic_id,
                    "metric_name": alert.metric_name,
                },
            )
            return _alert_from_row(row)

    def list_alerts(self, org_id: str) -> list[WatchlistAlert]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(WatchlistAlertRecord)
                .where(WatchlistAlertRecord.org_id == org_id)
                .order_by(WatchlistAlertRecord.created_at, WatchlistAlertRecord.id)
            ).all()
            return [_alert_from_row(row) for row in rows]


class TrendWorkflowRunner:
    """Deterministic Trend workflow runner (no Temporal dependency).

    Executes one run for one org: read active watchlist entries, compute the
    summary against the provided latest snapshots, persist alerts for breached
    topics, and audit each step. Snapshots come from the caller so this class
    stays pure-persistence + orchestration (no network, no LLM).
    """

    def __init__(self, store: WatchlistStore) -> None:
        self._store = store

    def execute(
        self,
        run: TrendWorkflowRun,
        latest_snapshots: dict[str, WatchlistSnapshot],
    ) -> tuple[TrendWorkflowRun, list[WatchlistAlert]]:
        started = datetime.now(UTC)
        with self._store._sessions.begin() as session:
            existing_run = session.scalars(
                sa.select(WatchlistRunRecord).where(WatchlistRunRecord.run_id == run.run_id)
            ).first()
            if existing_run is None:
                session.add(
                    WatchlistRunRecord(
                        run_id=run.run_id,
                        org_id=run.org_id,
                        status=RunStatus.RUNNING.value,
                        started_at=started,
                        finished_at=None,
                        error_class=None,
                    )
                )
                session.flush()
                audit.record(
                    "watchlists.run_started",
                    "watchlists",
                    f"trend_run:{run.run_id}",
                    {"org_id": run.org_id},
                )
        try:
            entries = self._store.list_entries(run.org_id, active_only=True)
            summary = watchlist_summary(entries, latest_snapshots)
            alerts: list[WatchlistAlert] = []
            existing_alerts = self._store.list_alerts(run.org_id)
            prior = {a.topic_id: a for a in existing_alerts if a.run_id == run.run_id}
            for row_summary in summary:
                prior_alert = prior.get(row_summary.topic_id)
                if prior_alert is not None and (
                    prior_alert.metric_name != (row_summary.metric_name or "threshold")
                    or prior_alert.metric_value != row_summary.metric_value
                    or prior_alert.threshold != row_summary.threshold
                ):
                    raise ValueError(
                        "alert already recorded for this run/topic with different fields: "
                        f"{run.run_id}/{row_summary.topic_id}"
                    )
                if not row_summary.breached or row_summary.metric_value is None:
                    continue
                if prior_alert is not None and (
                    prior_alert.metric_name != (row_summary.metric_name or "threshold")
                    or prior_alert.metric_value != row_summary.metric_value
                    or prior_alert.threshold != row_summary.threshold
                ):
                    raise ValueError(
                        "alert already recorded for this run/topic with different fields: "
                        f"{run.run_id}/{row_summary.topic_id}"
                    )
                alerts.append(
                    self._store.record_alert(
                        WatchlistAlert(
                            run_id=run.run_id,
                            topic_id=row_summary.topic_id,
                            org_id=run.org_id,
                            metric_name=row_summary.metric_name or "threshold",
                            metric_value=row_summary.metric_value,
                            threshold=row_summary.threshold,
                        )
                    )
                )
            finished = datetime.now(UTC)
            with self._store._sessions.begin() as session:
                stored = session.scalars(
                    sa.select(WatchlistRunRecord).where(WatchlistRunRecord.run_id == run.run_id)
                ).first()
                assert stored is not None
                stored.status = RunStatus.SUCCEEDED.value
                stored.finished_at = finished
                audit.record(
                    "watchlists.run_succeeded",
                    "watchlists",
                    f"trend_run:{run.run_id}",
                    {"org_id": run.org_id, "alerts": len(alerts)},
                )
            return (
                TrendWorkflowRun(
                    run_id=run.run_id,
                    org_id=run.org_id,
                    status=RunStatus.SUCCEEDED,
                    started_at=started,
                    finished_at=finished,
                    error_class=None,
                ),
                alerts,
            )
        except Exception as exc:
            error_class = type(exc).__name__[:128]
            with self._store._sessions.begin() as session:
                stored = session.scalars(
                    sa.select(WatchlistRunRecord).where(WatchlistRunRecord.run_id == run.run_id)
                ).first()
                if stored is not None:
                    stored.status = RunStatus.FAILED.value
                    stored.finished_at = datetime.now(UTC)
                    stored.error_class = error_class
                    audit.record(
                        "watchlists.run_failed",
                        "watchlists",
                        f"trend_run:{run.run_id}",
                        {"org_id": run.org_id, "error_class": error_class},
                    )
            raise


class WatchlistRunRecord(Base):
    """SQL row for one Trend workflow run."""

    __tablename__ = "trend_workflow_runs"
    __table_args__ = (
        sa.Index("uq_trend_workflow_runs_run_id", "run_id", unique=True),
        sa.Index("ix_trend_workflow_runs_org", "org_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    started_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    finished_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    error_class: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)


# ----------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ----------------------------------------------------------------------

router = APIRouter(tags=["watchlists"])


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> WatchlistStore:
    return WatchlistStore.from_engine(engine)


@router.post("/watchlists", response_model=WatchlistEntry, name="register_watchlist_entry")
def register_watchlist_entry_route(
    request: WatchlistEntry,
    store: Annotated[WatchlistStore, Depends(_store)],
) -> WatchlistEntry:
    try:
        return store.register_entry(request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/watchlists", response_model=list[WatchlistEntry], name="list_watchlist_entries")
def list_watchlist_entries_route(
    store: Annotated[WatchlistStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    include_inactive: Annotated[bool, Query()] = False,
) -> list[WatchlistEntry]:
    return store.list_entries(org_id, active_only=not include_inactive)


@router.post(
    "/watchlists/summary",
    response_model=list[WatchlistSummaryRow],
    name="watchlist_summary",
)
def watchlist_summary_route(
    store: Annotated[WatchlistStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    latest_snapshots: dict[str, WatchlistSnapshot],
) -> list[WatchlistSummaryRow]:
    entries = store.list_entries(org_id, active_only=True)
    return watchlist_summary(entries, latest_snapshots)


@router.post(
    "/trend-runs",
    response_model=TrendWorkflowRun,
    name="start_trend_run",
)
def start_trend_run_route(
    request: TrendWorkflowRun,
    store: Annotated[WatchlistStore, Depends(_store)],
    latest_snapshots: dict[str, WatchlistSnapshot],
) -> TrendWorkflowRun:
    runner = TrendWorkflowRunner(store)
    finished, _alerts = runner.execute(request, latest_snapshots)
    return finished


@router.get(
    "/watchlists/alerts",
    response_model=list[WatchlistAlert],
    name="list_watchlist_alerts",
)
def list_watchlist_alerts_route(
    store: Annotated[WatchlistStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[WatchlistAlert]:
    return store.list_alerts(org_id)
