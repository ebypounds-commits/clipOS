"""Health supervisor signals (CLIP-086, M21 opener).

PRD §K operations — one aggregated, operator-visible health view over
the running subsystems:

- **Signals** — `health_signals` rows are append-only observations,
  idempotent per (org_id, signal_id) with slug ids: a subsystem slug,
  a status (HEALTHY / DEGRADED / DOWN / UNKNOWN), a bounded detail,
  and a source (`LIVE` probes, `OPERATOR` manual input). Changed
  fields on the same signal_id conflict (409); audited
  `health_signals.recorded`.
- **Live probes** — `run_live_probes` exercises the running stack with
  short timeouts and returns one observation per subsystem without
  raising: `database` (SELECT 1), `cache` (Redis PING), `storage`
  (MinIO health endpoint), `workflow` (Temporal port connect). Probe
  failures degrade to a DOWN/DEGRADED signal — the supervisor never
  crashes. Env URLs come from the standard DATABASE_URL /
  CACHE_REDIS_URL / STORAGE_ENDPOINT / TEMPORAL_ADDRESS settings (no
  secrets in output, only statuses and error class names).
- **Aggregation** — `summarize_signals` is pure: latest observation
  per subsystem (ordered by created_at) and an overall status = the
  worst rank (DOWN > DEGRADED > UNKNOWN > HEALTHY).

Migration 0063 (CREATE-only). Routes (CLIP-033 pattern): POST
/health/probe (run + persist live signals, return summary), GET
/health/signals?org_id= (summary of latest), POST /health/signals
(operator-recorded signal). No LLM.
"""

from __future__ import annotations

import os
import socket
import urllib.request
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base

_STATUS_RANK = {
    "HEALTHY": 0,
    "UNKNOWN": 1,
    "DEGRADED": 2,
    "DOWN": 3,
}


class SignalStatus(str, Enum):
    """Health status of one subsystem observation."""

    HEALTHY = "HEALTHY"
    UNKNOWN = "UNKNOWN"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class HealthSignal(BaseModel):
    """One recorded health observation."""

    model_config = ConfigDict(frozen=True)

    signal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    subsystem: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    status: SignalStatus
    detail: str = Field(default="", max_length=255)
    source: str = Field(min_length=1, max_length=32)
    created_at: datetime | None = None


class SignalSummary(BaseModel):
    """Aggregated view: latest signal per subsystem + overall status."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    overall_status: SignalStatus
    signals: list[HealthSignal] = Field(default_factory=list)


class ProbeRequest(BaseModel):
    """Request body for one live probe run."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)


class RecordSignalRequest(BaseModel):
    """Request body for recording one operator-supplied signal."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    signal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    subsystem: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    status: SignalStatus
    detail: str = Field(default="", max_length=255)
    source: str = Field(default="OPERATOR", max_length=32)


class ProbeResult(BaseModel):
    """One raw live probe outcome (pre-persistence)."""

    model_config = ConfigDict(frozen=True)

    subsystem: str
    status: SignalStatus
    detail: str = Field(default="", max_length=255)


def summarize_signals(org_id: str, signals: list[HealthSignal]) -> SignalSummary:
    """Pure aggregation: latest signal per subsystem, worst-rank overall."""
    latest: dict[str, HealthSignal] = {}
    for signal in sorted(signals, key=lambda s: s.created_at or datetime.min.replace(tzinfo=UTC)):
        latest[signal.subsystem] = signal
    ordered = sorted(latest.values(), key=lambda s: s.subsystem)
    if not ordered:
        return SignalSummary(org_id=org_id, overall_status=SignalStatus.UNKNOWN, signals=[])
    worst = max(ordered, key=lambda s: _STATUS_RANK[s.status.value])
    return SignalSummary(org_id=org_id, overall_status=worst.status, signals=ordered)


def _probe_database(engine) -> ProbeResult:
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return ProbeResult(subsystem="database", status=SignalStatus.HEALTHY, detail="SELECT 1 ok")
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return ProbeResult(
            subsystem="database",
            status=SignalStatus.DOWN,
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
        )


def _probe_cache() -> ProbeResult:
    url = os.environ.get("CACHE_REDIS_URL", "")
    if not url:
        return ProbeResult(
            subsystem="cache",
            status=SignalStatus.UNKNOWN,
            detail="CACHE_REDIS_URL not set",
        )
    try:
        import redis  # local import: optional dependency

        client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
        return ProbeResult(subsystem="cache", status=SignalStatus.HEALTHY, detail="PING ok")
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return ProbeResult(
            subsystem="cache",
            status=SignalStatus.DEGRADED,
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
        )


def _probe_storage() -> ProbeResult:
    url = os.environ.get("STORAGE_ENDPOINT", "")
    if not url:
        return ProbeResult(
            subsystem="storage",
            status=SignalStatus.UNKNOWN,
            detail="STORAGE_ENDPOINT not set",
        )
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/minio/health/live", timeout=3) as resp:
            status_code = resp.status
        ok = 200 <= status_code < 300
        return ProbeResult(
            subsystem="storage",
            status=SignalStatus.HEALTHY if ok else SignalStatus.DEGRADED,
            detail="health endpoint ok" if ok else f"status {status_code}",
        )
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return ProbeResult(
            subsystem="storage",
            status=SignalStatus.DEGRADED,
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
        )


def _probe_workflow() -> ProbeResult:
    address = os.environ.get("TEMPORAL_ADDRESS", "")
    if not address:
        return ProbeResult(
            subsystem="workflow",
            status=SignalStatus.UNKNOWN,
            detail="TEMPORAL_ADDRESS not set",
        )
    host, _, port_text = address.partition(":")
    port = int(port_text or "7233")
    try:
        with socket.create_connection((host or "localhost", port), timeout=2):
            pass
        return ProbeResult(
            subsystem="workflow", status=SignalStatus.HEALTHY, detail="port reachable"
        )
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return ProbeResult(
            subsystem="workflow",
            status=SignalStatus.DEGRADED,
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
        )


def run_live_probes(engine) -> list[ProbeResult]:
    """Run every live probe; never raises."""
    return [
        _probe_database(engine),
        _probe_cache(),
        _probe_storage(),
        _probe_workflow(),
    ]


class HealthSignalRecord(Base):
    """SQL row for one health signal (append-only)."""

    __tablename__ = "health_signals"
    __table_args__ = (
        sa.Index(
            "uq_health_signals_identity",
            "org_id",
            "signal_id",
            unique=True,
        ),
        sa.Index("ix_health_signals_org_subsystem", "org_id", "subsystem"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    subsystem: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    detail: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    source: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _signal_from_row(row: HealthSignalRecord) -> HealthSignal:
    return HealthSignal(
        signal_id=row.signal_id,
        org_id=row.org_id,
        subsystem=row.subsystem,
        status=SignalStatus(row.status),
        detail=row.detail,
        source=row.source,
        created_at=_aware(row.created_at),
    )


class HealthStore:
    """Typed persistence for health signals."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> HealthStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def record_signal(self, request: RecordSignalRequest) -> HealthSignal:
        """Persist one signal; idempotent per (org, signal_id), conflict 409."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(HealthSignalRecord).where(
                    HealthSignalRecord.org_id == request.org_id,
                    HealthSignalRecord.signal_id == request.signal_id,
                )
            ).first()
            if existing is not None:
                stored = _signal_from_row(existing)
                if (
                    stored.subsystem != request.subsystem
                    or stored.status != request.status
                    or stored.detail != request.detail
                ):
                    raise ValueError(
                        f"health signal already recorded with different fields: {request.signal_id}"
                    )
                return stored
            row = HealthSignalRecord(
                signal_id=request.signal_id,
                org_id=request.org_id,
                subsystem=request.subsystem,
                status=request.status.value,
                detail=request.detail,
                source=request.source,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "health_signals.recorded",
                "health_signals",
                f"health_signal:{request.signal_id}",
                {
                    "org_id": request.org_id,
                    "subsystem": request.subsystem,
                    "status": request.status.value,
                    "source": request.source,
                },
            )
            return _signal_from_row(row)

    def record_probe_results(self, org_id: str, results: list[ProbeResult]) -> list[HealthSignal]:
        """Persist live probe outcomes under generated ids (uuid-based)."""
        signals: list[HealthSignal] = []
        for result in results:
            signal_id = str(uuid4())[:12]
            with self._sessions.begin() as session:
                row = HealthSignalRecord(
                    signal_id=signal_id,
                    org_id=org_id,
                    subsystem=result.subsystem,
                    status=result.status.value,
                    detail=result.detail,
                    source="LIVE",
                    created_at=datetime.now(UTC),
                )
                session.add(row)
                session.flush()
                audit.record(
                    "health_signals.recorded",
                    "health_signals",
                    f"health_signal:{signal_id}",
                    {
                        "org_id": org_id,
                        "subsystem": result.subsystem,
                        "status": result.status.value,
                        "source": "LIVE",
                    },
                )
                signals.append(_signal_from_row(row))
        return signals

    def list_signals(self, org_id: str, subsystem: str | None = None) -> list[HealthSignal]:
        with self._sessions() as session:
            stmt = sa.select(HealthSignalRecord).where(HealthSignalRecord.org_id == org_id)
            if subsystem is not None:
                stmt = stmt.where(HealthSignalRecord.subsystem == subsystem)
            rows = session.scalars(
                stmt.order_by(HealthSignalRecord.created_at, HealthSignalRecord.id)
            ).all()
            return [_signal_from_row(r) for r in rows]

    def summarize(self, org_id: str) -> SignalSummary:
        """Latest-per-subsystem aggregation over stored signals."""
        return summarize_signals(org_id, self.list_signals(org_id))


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["health"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> HealthStore:
    return HealthStore.from_engine(engine)


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


@router.post("/health/probe", response_model=SignalSummary, name="probe_health")
def probe_health_route(
    request: ProbeRequest,
    store: Annotated[HealthStore, Depends(_store)],
    engine: Annotated[Engine, Depends(_database_engine)],
) -> SignalSummary:
    """Run live probes, persist signals, return the aggregated summary."""
    results = run_live_probes(engine)
    store.record_probe_results(request.org_id, results)
    return store.summarize(request.org_id)


@router.get("/health/signals", response_model=SignalSummary, name="get_health_signals")
def get_health_signals_route(
    store: Annotated[HealthStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> SignalSummary:
    """Latest health signals per subsystem for one org."""
    return store.summarize(org_id)


@router.post("/health/signals", response_model=HealthSignal, name="record_health_signal")
def record_health_signal_route(
    request: RecordSignalRequest,
    store: Annotated[HealthStore, Depends(_store)],
) -> HealthSignal:
    """Record one operator-supplied health signal (idempotent)."""
    try:
        return store.record_signal(request)
    except ValueError as exc:
        raise _map_error(exc) from exc
