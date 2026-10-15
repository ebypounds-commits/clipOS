"""Credential/quota/disk/provider health monitors (CLIP-089).

PRD §K operations — the four resource-exhaustion monitors called out
for operator attention, each evaluated by a pure function and reported
through one append-only signal table:

- **Credential expiry** — `check_credential_expiry(days_left, warn_days)`
  maps days-until-expiry to HEALTHY / DEGRADED (inside warn window) /
  DOWN (already expired). Credentials are referenced by opaque
  `credential_ref` (vault reference) — never a secret value.
- **Quota headroom** — `check_quota_headroom(used, limit, warn_ratio)`
  reports DEGRADED once usage crosses `warn_ratio` (default 0.8) and
  DOWN at/over the limit; zero/negative limits are DOWN
  (`limit_invalid`) rather than dividing by zero.
- **Disk usage** — `check_disk_usage(used_bytes, total_bytes,
  warn_ratio)` mirrors quota semantics over bytes; total <= 0 is DOWN.
- **Provider health** — `check_provider_health(ok, detail)` maps an
  adapter/probe boolean to HEALTHY/DOWN while keeping the detail text
  bounded (secrets are never placed here by callers; the function only
  truncates).

`monitor_checks` (migration 0066) is the append-only record: unique
(org, check_id), subsystem slug, monitor kind, measured value and
threshold, resulting status and detail. Supplying the same check_id
with different fields conflicts (409); audited
`monitor_checks.recorded`. `summarize_monitors` is pure: the latest
check per monitor kind plus `attention` = every monitor not HEALTHY
and the worst-rank overall status, so one call answers "what needs
attention right now".

Routes (CLIP-033 pattern): POST /monitors/check (evaluate one monitor
and record it), GET /monitors?org_id= (summary + attention list), GET
/monitors/checks?org_id=&monitor_kind= (raw history). No LLM, no
network — callers supply measurements from their own probes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base

DEFAULT_WARN_RATIO = 0.8
_STATUS_RANK = {"HEALTHY": 0, "UNKNOWN": 1, "DEGRADED": 2, "DOWN": 3}


class MonitorStatus(str, Enum):
    """Health status produced by one monitor check."""

    HEALTHY = "HEALTHY"
    UNKNOWN = "UNKNOWN"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class MonitorKind(str, Enum):
    """The four monitored resource classes (PRD §K)."""

    CREDENTIAL_EXPIRY = "CREDENTIAL_EXPIRY"
    QUOTA_HEADROOM = "QUOTA_HEADROOM"
    DISK_USAGE = "DISK_USAGE"
    PROVIDER_HEALTH = "PROVIDER_HEALTH"


class MonitorCheck(BaseModel):
    """One recorded monitor evaluation."""

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    subsystem: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    monitor_kind: MonitorKind
    subject_ref: str = Field(min_length=1, max_length=255)
    status: MonitorStatus
    measured: float | None = Field(default=None)
    threshold: float | None = Field(default=None)
    detail: str = Field(default="", max_length=255)
    created_at: datetime | None = None


class MonitorSummary(BaseModel):
    """Latest check per monitor kind plus the attention list."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    overall_status: MonitorStatus
    monitors: list[MonitorCheck] = Field(default_factory=list)
    attention: list[MonitorCheck] = Field(default_factory=list)


class MonitorResult(BaseModel):
    """Pure monitor outcome (pre-persistence)."""

    model_config = ConfigDict(frozen=True)

    status: MonitorStatus
    detail: str = Field(min_length=1, max_length=255)
    measured: float | None = None
    threshold: float | None = None


def check_credential_expiry(days_left: float, warn_days: float = 14.0) -> MonitorResult:
    """Pure: expiry proximity (never sees the credential itself).

    warn_days <= 0 is treated as the 14-day default so a caller cannot
    accidentally disable the warning window.
    """
    if warn_days <= 0:
        warn_days = 14.0
    if days_left < 0:
        return MonitorResult(
            status=MonitorStatus.DOWN,
            detail="credential expired",
            measured=days_left,
            threshold=warn_days,
        )
    if days_left <= warn_days:
        return MonitorResult(
            status=MonitorStatus.DEGRADED,
            detail=f"credential expires in {days_left:.1f}d",
            measured=days_left,
            threshold=warn_days,
        )
    return MonitorResult(
        status=MonitorStatus.HEALTHY,
        detail=f"credential valid for {days_left:.1f}d",
        measured=days_left,
        threshold=warn_days,
    )


def check_quota_headroom(
    used: float, limit: float, warn_ratio: float = DEFAULT_WARN_RATIO
) -> MonitorResult:
    """Pure: quota usage vs limit (limit <= 0 is invalid, not division)."""
    if limit <= 0:
        return MonitorResult(
            status=MonitorStatus.DOWN,
            detail="limit_invalid",
            measured=used,
            threshold=limit,
        )
    ratio = used / limit
    percent = ratio * 100.0
    if ratio >= 1.0:
        return MonitorResult(
            status=MonitorStatus.DOWN,
            detail=f"quota exhausted ({percent:.0f}%)",
            measured=used,
            threshold=limit,
        )
    if ratio >= warn_ratio:
        return MonitorResult(
            status=MonitorStatus.DEGRADED,
            detail=f"quota {percent:.0f}% used",
            measured=used,
            threshold=limit,
        )
    return MonitorResult(
        status=MonitorStatus.HEALTHY,
        detail=f"quota {percent:.0f}% used",
        measured=used,
        threshold=limit,
    )


def check_disk_usage(
    used_bytes: float, total_bytes: float, warn_ratio: float = DEFAULT_WARN_RATIO
) -> MonitorResult:
    """Pure: disk pressure in bytes (total <= 0 is DOWN)."""
    if total_bytes <= 0:
        return MonitorResult(
            status=MonitorStatus.DOWN,
            detail="capacity_unknown",
            measured=used_bytes,
            threshold=total_bytes,
        )
    ratio = used_bytes / total_bytes
    result = check_quota_headroom(used_bytes, total_bytes, warn_ratio)
    if result.status is MonitorStatus.HEALTHY or result.status is MonitorStatus.DEGRADED:
        detail = f"disk {ratio * 100:.0f}% used"
    else:
        detail = "disk full" if ratio >= 1.0 else result.detail
    return MonitorResult(
        status=result.status,
        detail=detail[:255],
        measured=used_bytes,
        threshold=total_bytes,
    )


def check_provider_health(ok: bool, detail: str = "") -> MonitorResult:
    """Pure: provider probe boolean -> status (detail truncated only)."""
    text = detail.strip()[:200] or ("provider reachable" if ok else "provider unreachable")
    return MonitorResult(status=MonitorStatus.HEALTHY if ok else MonitorStatus.DOWN, detail=text)


def summarize_monitors(org_id: str, checks: list[MonitorCheck]) -> MonitorSummary:
    """Pure: latest check per kind, worst-rank overall, attention list."""
    latest: dict[str, MonitorCheck] = {}
    for check in sorted(checks, key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC)):
        latest[check.monitor_kind.value] = check
    monitors = sorted(latest.values(), key=lambda c: c.monitor_kind.value)
    if not monitors:
        return MonitorSummary(
            org_id=org_id,
            overall_status=MonitorStatus.UNKNOWN,
            monitors=[],
            attention=[],
        )
    worst = max(monitors, key=lambda c: _STATUS_RANK[c.status.value])
    attention = [m for m in monitors if m.status is not MonitorStatus.HEALTHY]
    return MonitorSummary(
        org_id=org_id,
        overall_status=worst.status,
        monitors=monitors,
        attention=attention,
    )


class MonitorCheckRecord(Base):
    """SQL row for one monitor check (append-only)."""

    __tablename__ = "monitor_checks"
    __table_args__ = (
        sa.Index("uq_monitor_checks_identity", "org_id", "check_id", unique=True),
        sa.Index("ix_monitor_checks_org_kind", "org_id", "monitor_kind"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    check_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    subsystem: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    monitor_kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    subject_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    measured: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    detail: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class RecordCheckRequest(BaseModel):
    """Request body for one monitor evaluation."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    check_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    subsystem: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    monitor_kind: MonitorKind
    subject_ref: str = Field(min_length=1, max_length=255)
    days_left: float | None = None
    used: float | None = None
    limit: float | None = None
    warn_ratio: float = Field(default=DEFAULT_WARN_RATIO, gt=0.0, le=1.0)
    provider_ok: bool | None = None
    provider_detail: str = Field(default="", max_length=255)


def evaluate_monitor(request: RecordCheckRequest) -> MonitorResult:
    """Pure dispatcher: run the monitor named by monitor_kind."""
    kind = request.monitor_kind
    if kind is MonitorKind.CREDENTIAL_EXPIRY:
        if request.days_left is None:
            raise ValueError("days_left required for CREDENTIAL_EXPIRY")
        return check_credential_expiry(request.days_left, warn_days=request.warn_ratio * 100)
    if kind is MonitorKind.QUOTA_HEADROOM:
        if request.used is None or request.limit is None:
            raise ValueError("used and limit required for QUOTA_HEADROOM")
        return check_quota_headroom(request.used, request.limit, request.warn_ratio)
    if kind is MonitorKind.DISK_USAGE:
        if request.used is None or request.limit is None:
            raise ValueError("used and limit required for DISK_USAGE")
        return check_disk_usage(request.used, request.limit, request.warn_ratio)
    if request.provider_ok is None:
        raise ValueError("provider_ok required for PROVIDER_HEALTH")
    return check_provider_health(request.provider_ok, request.provider_detail)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _check_from_row(row: MonitorCheckRecord) -> MonitorCheck:
    return MonitorCheck(
        check_id=row.check_id,
        org_id=row.org_id,
        subsystem=row.subsystem,
        monitor_kind=MonitorKind(row.monitor_kind),
        subject_ref=row.subject_ref,
        status=MonitorStatus(row.status),
        measured=row.measured,
        threshold=row.threshold,
        detail=row.detail,
        created_at=_aware(row.created_at),
    )


class MonitorStore:
    """Typed persistence for monitor checks."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> MonitorStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def record_check(self, request: RecordCheckRequest) -> MonitorCheck:
        """Evaluate one monitor and append the result (conflict 409)."""
        result = evaluate_monitor(request)
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(MonitorCheckRecord).where(
                    MonitorCheckRecord.org_id == request.org_id,
                    MonitorCheckRecord.check_id == request.check_id,
                )
            ).first()
            if existing is not None:
                stored = _check_from_row(existing)
                if (
                    stored.monitor_kind != request.monitor_kind
                    or stored.subsystem != request.subsystem
                    or stored.subject_ref != request.subject_ref
                    or stored.status != result.status
                    or stored.detail != result.detail
                ):
                    raise ValueError(
                        f"monitor check already recorded with different fields: {request.check_id}"
                    )
                return stored
            row = MonitorCheckRecord(
                check_id=request.check_id,
                org_id=request.org_id,
                subsystem=request.subsystem,
                monitor_kind=request.monitor_kind.value,
                subject_ref=request.subject_ref,
                status=result.status.value,
                measured=result.measured,
                threshold=result.threshold,
                detail=result.detail,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "monitor_checks.recorded",
                "monitor_checks",
                f"monitor_check:{request.check_id}",
                {
                    "org_id": request.org_id,
                    "monitor_kind": request.monitor_kind.value,
                    "subsystem": request.subsystem,
                    "status": result.status.value,
                },
            )
            return _check_from_row(row)

    def list_checks(
        self, org_id: str, monitor_kind: MonitorKind | None = None
    ) -> list[MonitorCheck]:
        with self._sessions() as session:
            stmt = sa.select(MonitorCheckRecord).where(MonitorCheckRecord.org_id == org_id)
            if monitor_kind is not None:
                stmt = stmt.where(MonitorCheckRecord.monitor_kind == monitor_kind.value)
            rows = session.scalars(
                stmt.order_by(MonitorCheckRecord.created_at, MonitorCheckRecord.id)
            ).all()
            return [_check_from_row(r) for r in rows]

    def summarize(self, org_id: str) -> MonitorSummary:
        return summarize_monitors(org_id, self.list_checks(org_id))


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["monitors"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> MonitorStore:
    return MonitorStore.from_engine(engine)


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


@router.post("/monitors/check", response_model=MonitorCheck, name="record_monitor_check")
def record_monitor_check_route(
    request: RecordCheckRequest,
    store: Annotated[MonitorStore, Depends(_store)],
) -> MonitorCheck:
    """Evaluate one monitor and record the result (idempotent)."""
    try:
        return store.record_check(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/monitors", response_model=MonitorSummary, name="get_monitor_summary")
def get_monitor_summary_route(
    store: Annotated[MonitorStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> MonitorSummary:
    """Latest check per monitor kind plus the attention list."""
    return store.summarize(org_id)


@router.get(
    "/monitors/checks",
    response_model=list[MonitorCheck],
    name="list_monitor_checks",
)
def list_monitor_checks_route(
    store: Annotated[MonitorStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    monitor_kind: Annotated[MonitorKind | None, Query()] = None,
) -> list[MonitorCheck]:
    """Raw monitor history for one org, optionally by kind."""
    return store.list_checks(org_id, monitor_kind)
