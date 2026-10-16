"""Encrypted backup and restore verification (CLIP-096).

The operations runbook's backup policy baseline asks for:

- a **daily encrypted full backup**;
- a **weekly automated restore test in an isolated environment**;
- a documented monthly disaster-recovery drill.

CLIP-013 shipped the manifest primitives and explicitly left encryption and
restore testing open; CLIP-099's predecessor milestone closes that gap here.

- **Backup artifacts** — `backup_artifacts` rows are org-scoped with client
  slug ids, idempotent per (org_id, artifact_id). Each records the artifact
  kind, the key *reference* (never a key), the key fingerprint, the
  ciphertext digest and byte counts, and the plaintext digest so a restore
  can be proven to reproduce the original bytes. Audited
  `backup_artifacts.recorded`.
- **Restore tests** — `restore_tests` rows are append-only, org-scoped and
  idempotent per (org_id, test_id). Each records the artifact restored, an
  isolated-environment reference, whether the restored bytes matched the
  recorded plaintext digest, the wall-clock restore duration, and the
  measured RPO (data-loss window). Audited `restore_tests.recorded`.
- **Policy evaluation** — pure functions, not stored judgement:

  * :func:`evaluate_restore` decides PASS/FAIL from digest equality plus
    the duration, so a restore that reproduces the bytes but blows the RTO
    is reported as a distinct failure mode;
  * :func:`check_backup_freshness` maps age-against-cadence to
    HEALTHY/DEGRADED/DOWN, mirroring the monitor semantics the runbook
    already uses for every other unattended check;
  * :func:`restore_test_due` applies the weekly cadence;
  * :func:`evaluate_drill_cadence` applies the monthly drill cadence.

Routes (CLIP-033 pattern): POST/GET /backups/artifacts,
POST/GET /backups/restore-tests, GET /backups/status. No LLM, no network —
callers supply the artifact digests and restore observations from their own
runners.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from clipos_api.monitors import MonitorStatus
from clipos_api.repository import Base

DAILY_CADENCE_HOURS = 24.0
WEEKLY_CADENCE_DAYS = 7.0
MONTHLY_CADENCE_DAYS = 30.0
DEFAULT_FRESHNESS_WARN_HOURS = 30.0
DEFAULT_RESTORE_RTO_SECONDS = 3600.0


class ArtifactKind(str, Enum):
    """Backup artifact classes (runbook §Backup policy baseline)."""

    POSTGRES = "POSTGRES"
    OBJECT_STORAGE = "OBJECT_STORAGE"
    CONFIGURATION = "CONFIGURATION"
    FULL = "FULL"


class RestoreOutcome(str, Enum):
    """Result of one restore test."""

    PASS = "PASS"
    FAIL = "FAIL"


class BackupArtifact(BaseModel):
    """One recorded encrypted backup artifact."""

    model_config = ConfigDict(from_attributes=True)

    artifact_id: str
    org_id: str
    kind: ArtifactKind
    key_ref: str
    key_fingerprint: str
    plaintext_sha256: str
    ciphertext_sha256: str
    plaintext_bytes: int
    ciphertext_bytes: int
    created_at: datetime


class RestoreTest(BaseModel):
    """One recorded restore test (append-only)."""

    model_config = ConfigDict(from_attributes=True)

    test_id: str
    org_id: str
    artifact_id: str
    environment_ref: str
    outcome: RestoreOutcome
    digest_matched: bool
    duration_seconds: float
    rpo_seconds: float
    detail: str
    created_at: datetime


class RestoreEvaluation(BaseModel):
    """Pure restore-test verdict (pre-persistence)."""

    model_config = ConfigDict(frozen=True)

    outcome: RestoreOutcome
    digest_matched: bool
    detail: str
    rto_seconds: float


class BackupStatus(BaseModel):
    """Aggregate backup posture for one org."""

    model_config = ConfigDict(frozen=True)

    org_id: str
    freshness: MonitorStatus
    last_artifact_at: datetime | None
    last_restore_outcome: RestoreOutcome | None
    last_restore_at: datetime | None
    restore_test_due: bool
    drill_due: bool
    attention: list[str] = Field(default_factory=list)


class RecordArtifactRequest(BaseModel):
    """Record one encrypted backup artifact."""

    artifact_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    kind: ArtifactKind
    key_ref: str = Field(min_length=1, max_length=128)
    key_fingerprint: str = Field(min_length=1, max_length=64)
    plaintext_sha256: str = Field(min_length=64, max_length=64)
    ciphertext_sha256: str = Field(min_length=64, max_length=64)
    plaintext_bytes: int = Field(ge=0)
    ciphertext_bytes: int = Field(ge=0)


class RecordRestoreTestRequest(BaseModel):
    """Record one restore test."""

    test_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    artifact_id: str = Field(min_length=1, max_length=128)
    environment_ref: str = Field(min_length=1, max_length=255)
    digest_matched: bool
    duration_seconds: float = Field(ge=0)
    rpo_seconds: float = Field(ge=0)
    detail: str = Field(default="", max_length=1000)
    rto_seconds: float = Field(default=DEFAULT_RESTORE_RTO_SECONDS, gt=0)


class BackupArtifactRecord(Base):
    """SQL row for one encrypted backup artifact."""

    __tablename__ = "backup_artifacts"
    __table_args__ = (
        sa.Index(
            "uq_backup_artifacts_org_artifact",
            "org_id",
            "artifact_id",
            unique=True,
        ),
        sa.Index("ix_backup_artifacts_org_kind", "org_id", "kind"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    key_ref: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    plaintext_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    ciphertext_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    plaintext_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    ciphertext_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class RestoreTestRecord(Base):
    """SQL row for one restore test (append-only)."""

    __tablename__ = "restore_tests"
    __table_args__ = (
        sa.Index("uq_restore_tests_org_test", "org_id", "test_id", unique=True),
        sa.Index("ix_restore_tests_org_artifact", "org_id", "artifact_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    test_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    environment_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    digest_matched: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(sa.Float, nullable=False)
    rpo_seconds: Mapped[float] = mapped_column(sa.Float, nullable=False)
    detail: Mapped[str] = mapped_column(sa.String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def evaluate_restore(
    digest_matched: bool,
    duration_seconds: float,
    rto_seconds: float = DEFAULT_RESTORE_RTO_SECONDS,
) -> RestoreEvaluation:
    """Pure: restore verdict from digest equality and duration.

    The two failure modes are distinct on purpose — a restore that
    reproduces the bytes but exceeds the RTO is not the same operational
    problem as one that restores the wrong data.
    """
    if not digest_matched:
        return RestoreEvaluation(
            outcome=RestoreOutcome.FAIL,
            digest_matched=False,
            detail="restored bytes did not match the recorded plaintext digest",
            rto_seconds=rto_seconds,
        )
    if duration_seconds > rto_seconds:
        return RestoreEvaluation(
            outcome=RestoreOutcome.FAIL,
            digest_matched=True,
            detail=(f"restore exceeded RTO: {duration_seconds:.1f}s > {rto_seconds:.1f}s"),
            rto_seconds=rto_seconds,
        )
    return RestoreEvaluation(
        outcome=RestoreOutcome.PASS,
        digest_matched=True,
        detail=f"restored and verified in {duration_seconds:.1f}s",
        rto_seconds=rto_seconds,
    )


def check_backup_freshness(
    now: datetime,
    last_artifact_at: datetime | None,
    warn_hours: float = DEFAULT_FRESHNESS_WARN_HOURS,
    cadence_hours: float = DAILY_CADENCE_HOURS,
) -> MonitorStatus:
    """Pure: backup age against the daily cadence."""
    if last_artifact_at is None:
        return MonitorStatus.DOWN
    age_hours = (now - _aware(last_artifact_at)).total_seconds() / 3600.0
    if age_hours <= cadence_hours:
        return MonitorStatus.HEALTHY
    if age_hours <= warn_hours:
        return MonitorStatus.DEGRADED
    return MonitorStatus.DOWN


def restore_test_due(
    now: datetime,
    last_restore_at: datetime | None,
    cadence_days: float = WEEKLY_CADENCE_DAYS,
) -> bool:
    """Pure: is the weekly automated restore test due?"""
    if last_restore_at is None:
        return True
    age_days = (now - _aware(last_restore_at)).total_seconds() / 86400.0
    return age_days >= cadence_days


def evaluate_drill_cadence(
    now: datetime,
    last_drill_at: datetime | None,
    cadence_days: float = MONTHLY_CADENCE_DAYS,
) -> bool:
    """Pure: is the documented disaster-recovery drill due?"""
    if last_drill_at is None:
        return True
    age_days = (now - _aware(last_drill_at)).total_seconds() / 86400.0
    return age_days >= cadence_days


def summarize_backup_status(
    org_id: str,
    artifacts: list[BackupArtifact],
    tests: list[RestoreTest],
    now: datetime,
) -> BackupStatus:
    """Pure: combine freshness, last restore outcome, and cadence dues."""
    last_artifact_at = max((a.created_at for a in artifacts), default=None)
    last_test = max(tests, key=lambda t: t.created_at, default=None)
    freshness = check_backup_freshness(now, last_artifact_at)
    due = restore_test_due(now, last_test.created_at if last_test else None)
    drill = evaluate_drill_cadence(now, last_test.created_at if last_test else None)

    attention: list[str] = []
    if freshness is not MonitorStatus.HEALTHY:
        attention.append(f"backup freshness is {freshness.value}")
    if last_test is not None and last_test.outcome is RestoreOutcome.FAIL:
        attention.append(f"last restore test failed: {last_test.detail}")
    if due:
        attention.append("weekly restore test is due")
    if drill:
        attention.append("monthly disaster-recovery drill is due")

    return BackupStatus(
        org_id=org_id,
        freshness=freshness,
        last_artifact_at=last_artifact_at,
        last_restore_outcome=last_test.outcome if last_test else None,
        last_restore_at=last_test.created_at if last_test else None,
        restore_test_due=due,
        drill_due=drill,
        attention=attention,
    )


def _artifact_from_row(row: BackupArtifactRecord) -> BackupArtifact:
    return BackupArtifact(
        artifact_id=row.artifact_id,
        org_id=row.org_id,
        kind=ArtifactKind(row.kind),
        key_ref=row.key_ref,
        key_fingerprint=row.key_fingerprint,
        plaintext_sha256=row.plaintext_sha256,
        ciphertext_sha256=row.ciphertext_sha256,
        plaintext_bytes=row.plaintext_bytes,
        ciphertext_bytes=row.ciphertext_bytes,
        created_at=_aware(row.created_at),
    )


def _test_from_row(row: RestoreTestRecord) -> RestoreTest:
    return RestoreTest(
        test_id=row.test_id,
        org_id=row.org_id,
        artifact_id=row.artifact_id,
        environment_ref=row.environment_ref,
        outcome=RestoreOutcome(row.outcome),
        digest_matched=row.digest_matched,
        duration_seconds=row.duration_seconds,
        rpo_seconds=row.rpo_seconds,
        detail=row.detail,
        created_at=_aware(row.created_at),
    )


class BackupStore:
    """Typed persistence for backup artifacts and restore tests."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> BackupStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # -- artifacts ---------------------------------------------------------

    def record_artifact(self, request: RecordArtifactRequest) -> BackupArtifact:
        """Idempotent per (org, artifact_id)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(BackupArtifactRecord).where(
                    BackupArtifactRecord.org_id == request.org_id,
                    BackupArtifactRecord.artifact_id == request.artifact_id,
                )
            ).first()
            if existing is not None:
                stored = _artifact_from_row(existing)
                if (
                    stored.kind is not request.kind
                    or stored.plaintext_sha256 != request.plaintext_sha256
                    or stored.ciphertext_sha256 != request.ciphertext_sha256
                    or stored.key_fingerprint != request.key_fingerprint
                ):
                    raise ValueError(
                        f"backup artifact already recorded with different fields: "
                        f"{request.artifact_id}"
                    )
                return stored

            row = BackupArtifactRecord(
                artifact_id=request.artifact_id,
                org_id=request.org_id,
                kind=request.kind.value,
                key_ref=request.key_ref,
                key_fingerprint=request.key_fingerprint,
                plaintext_sha256=request.plaintext_sha256,
                ciphertext_sha256=request.ciphertext_sha256,
                plaintext_bytes=request.plaintext_bytes,
                ciphertext_bytes=request.ciphertext_bytes,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "backup_artifacts.recorded",
                "backup-store",
                f"artifact:{request.artifact_id}",
                {
                    "org_id": request.org_id,
                    "kind": request.kind.value,
                    "key_ref": request.key_ref,
                    "key_fingerprint": request.key_fingerprint,
                    "plaintext_bytes": request.plaintext_bytes,
                    "ciphertext_bytes": request.ciphertext_bytes,
                },
            )
            return _artifact_from_row(row)

    def get_artifact(self, artifact_id: str, org_id: str) -> BackupArtifact | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(BackupArtifactRecord).where(
                    BackupArtifactRecord.org_id == org_id,
                    BackupArtifactRecord.artifact_id == artifact_id,
                )
            ).first()
            return _artifact_from_row(row) if row is not None else None

    def list_artifacts(self, org_id: str, kind: ArtifactKind | None = None) -> list[BackupArtifact]:
        with self._sessions() as session:
            statement = sa.select(BackupArtifactRecord).where(BackupArtifactRecord.org_id == org_id)
            if kind is not None:
                statement = statement.where(BackupArtifactRecord.kind == kind.value)
            rows = session.scalars(statement.order_by(BackupArtifactRecord.id)).all()
            return [_artifact_from_row(row) for row in rows]

    # -- restore tests -----------------------------------------------------

    def record_restore_test(self, request: RecordRestoreTestRequest) -> RestoreTest:
        """Append-only, idempotent per (org, test_id).

        The artifact must exist in the same org; the outcome is computed
        from the supplied observations rather than trusted from the caller.
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(RestoreTestRecord).where(
                    RestoreTestRecord.org_id == request.org_id,
                    RestoreTestRecord.test_id == request.test_id,
                )
            ).first()
            if existing is not None:
                stored = _test_from_row(existing)
                if (
                    stored.artifact_id != request.artifact_id
                    or stored.digest_matched != request.digest_matched
                    or stored.duration_seconds != request.duration_seconds
                ):
                    raise ValueError(
                        f"restore test already recorded with different fields: {request.test_id}"
                    )
                return stored

            artifact = session.scalars(
                sa.select(BackupArtifactRecord).where(
                    BackupArtifactRecord.org_id == request.org_id,
                    BackupArtifactRecord.artifact_id == request.artifact_id,
                )
            ).first()
            if artifact is None:
                raise ValueError(f"backup artifact not found: {request.artifact_id}")

            evaluation = evaluate_restore(
                request.digest_matched, request.duration_seconds, request.rto_seconds
            )
            detail = request.detail or evaluation.detail
            row = RestoreTestRecord(
                test_id=request.test_id,
                org_id=request.org_id,
                artifact_id=request.artifact_id,
                environment_ref=request.environment_ref,
                outcome=evaluation.outcome.value,
                digest_matched=request.digest_matched,
                duration_seconds=request.duration_seconds,
                rpo_seconds=request.rpo_seconds,
                detail=detail,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "restore_tests.recorded",
                "backup-store",
                f"restore_test:{request.test_id}",
                {
                    "org_id": request.org_id,
                    "artifact_id": request.artifact_id,
                    "outcome": evaluation.outcome.value,
                    "digest_matched": request.digest_matched,
                    "duration_seconds": request.duration_seconds,
                    "environment_ref": request.environment_ref,
                },
            )
            return _test_from_row(row)

    def list_restore_tests(self, org_id: str) -> list[RestoreTest]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(RestoreTestRecord)
                .where(RestoreTestRecord.org_id == org_id)
                .order_by(RestoreTestRecord.id)
            ).all()
            return [_test_from_row(row) for row in rows]

    def status(self, org_id: str, now: datetime | None = None) -> BackupStatus:
        return summarize_backup_status(
            org_id,
            self.list_artifacts(org_id),
            self.list_restore_tests(org_id),
            now or datetime.now(UTC),
        )


router = APIRouter(tags=["backups"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> BackupStore:
    return BackupStore.from_engine(engine)


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


@router.post("/backups/artifacts", response_model=BackupArtifact, name="record_backup_artifact")
def record_artifact_route(
    request: RecordArtifactRequest,
    store: Annotated[BackupStore, Depends(_store)],
) -> BackupArtifact:
    """Record one encrypted backup artifact (idempotent)."""
    try:
        return store.record_artifact(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/backups/artifacts", response_model=list[BackupArtifact], name="list_backup_artifacts")
def list_artifacts_route(
    store: Annotated[BackupStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    kind: Annotated[ArtifactKind | None, Query()] = None,
) -> list[BackupArtifact]:
    """List backup artifacts for one org."""
    return store.list_artifacts(org_id, kind)


@router.get(
    "/backups/artifacts/{artifact_id}",
    response_model=BackupArtifact,
    name="get_backup_artifact",
)
def get_artifact_route(
    artifact_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[BackupStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> BackupArtifact:
    """Get one backup artifact by id and org."""
    stored = store.get_artifact(artifact_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"backup artifact not found: {artifact_id}")
    return stored


@router.post("/backups/restore-tests", response_model=RestoreTest, name="record_restore_test")
def record_restore_test_route(
    request: RecordRestoreTestRequest,
    store: Annotated[BackupStore, Depends(_store)],
) -> RestoreTest:
    """Record one restore test (idempotent; outcome is computed)."""
    try:
        return store.record_restore_test(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/backups/restore-tests", response_model=list[RestoreTest], name="list_restore_tests")
def list_restore_tests_route(
    store: Annotated[BackupStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[RestoreTest]:
    """List restore tests for one org."""
    return store.list_restore_tests(org_id)


@router.get("/backups/status", response_model=BackupStatus, name="backup_status")
def backup_status_route(
    store: Annotated[BackupStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> BackupStatus:
    """Backup freshness, last restore outcome, and cadence dues."""
    return store.status(org_id)


__all__ = [
    "DAILY_CADENCE_HOURS",
    "DEFAULT_FRESHNESS_WARN_HOURS",
    "DEFAULT_RESTORE_RTO_SECONDS",
    "MONTHLY_CADENCE_DAYS",
    "WEEKLY_CADENCE_DAYS",
    "ArtifactKind",
    "BackupArtifact",
    "BackupStatus",
    "BackupStore",
    "RecordArtifactRequest",
    "RecordRestoreTestRequest",
    "RestoreEvaluation",
    "RestoreOutcome",
    "RestoreTest",
    "check_backup_freshness",
    "evaluate_drill_cadence",
    "evaluate_restore",
    "restore_test_due",
    "router",
    "summarize_backup_status",
]
