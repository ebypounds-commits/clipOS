"""Deterministic evaluation runs, activation gating, and execution audit.

Evaluation records are idempotent by ``(agent_id, version, evaluator, run_id)``.
The activation gate permits promotion only when every evaluator required by the
immutable version has a passing, recent run.  Execution audit records capture
when a registered agent version ran, using logical model aliases only.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.agent_registry import AgentVersionRecord, database_engine
from clipos_api.repository import Base

_LOGICAL_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_DEFAULT_RECENCY_WINDOW_DAYS = 30
_MAX_LIMIT = 200


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class EvaluationRun(BaseModel):
    """A deterministic evaluator result for one immutable agent version."""

    model_config = ConfigDict(frozen=True)

    agent_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    evaluator: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=1, max_length=128)
    score: float = Field(ge=0, le=1)
    passed: bool
    at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class RecordEvaluationRequest(BaseModel):
    """Payload for recording one evaluator run; pass/fail is derived server-side."""

    model_config = ConfigDict(frozen=True)

    version: str = Field(min_length=1, max_length=128)
    evaluator: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=1, max_length=128)
    score: float = Field(ge=0, le=1)
    at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ExecutionOutcome(str, Enum):
    """Lifecycle of one audited execution."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExecutionAuditRecord(BaseModel):
    """Operator-visible record that one agent version executed once."""

    model_config = ConfigDict(frozen=True)

    agent_id: str = Field(min_length=1, max_length=128)
    execution_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    model_alias: str = Field(min_length=1, max_length=255)
    action: str = Field(min_length=1, max_length=255)
    started_at: datetime
    finished_at: datetime | None = None
    outcome: ExecutionOutcome = ExecutionOutcome.RUNNING
    error_class: str | None = Field(default=None, max_length=255)


class EvaluationRunRecord(Base):
    """SQL row for one evaluator run; identity is the natural business key."""

    __tablename__ = "agent_evaluation_runs"
    __table_args__ = (
        sa.UniqueConstraint(
            "agent_id",
            "version",
            "evaluator",
            "run_id",
            name="uq_agent_evaluation_runs_identity",
        ),
        sa.Index("ix_agent_evaluation_runs_lookup", "agent_id", "version", "evaluator", "at"),
    )

    run_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    version: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    evaluator: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    passed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)


class ExecutionAuditRecordModel(Base):
    """SQL row for the execution audit trail; one row per execution id."""

    __tablename__ = "agent_execution_audits"
    __table_args__ = (
        sa.UniqueConstraint("agent_id", "execution_id", name="uq_agent_executions_identity"),
        sa.Index("ix_agent_execution_audits_lookup", "agent_id", "started_at"),
    )

    execution_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    version: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    model_alias: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    error_class: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    started_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    finished_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=True)


def _run_from_row(row: EvaluationRunRecord) -> EvaluationRun:
    return EvaluationRun(
        agent_id=row.agent_id,
        version=row.version,
        evaluator=row.evaluator,
        run_id=row.run_id,
        score=row.score,
        passed=row.passed,
        at=_aware(row.at),
        details=row.details,
    )


class AgentEvaluationStore:
    """Session-bound persistence for evaluation runs and execution audits."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> AgentEvaluationStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def record_evaluation(
        self,
        agent_id: str,
        request: RecordEvaluationRequest,
    ) -> EvaluationRun:
        """Idempotently record an evaluator run against a registered version."""
        with self._sessions.begin() as session:
            return self.record_evaluation_in_transaction(session, agent_id, request)

    def record_evaluation_in_transaction(
        self, session: Session, agent_id: str, request: RecordEvaluationRequest
    ) -> EvaluationRun:
        version_record = session.get(
            AgentVersionRecord, {"agent_id": agent_id, "version": request.version}
        )
        if version_record is None:
            raise ValueError(f"agent version not found: {agent_id}:{request.version}")
        requirements = {
            item["name"]: float(item["required_score"])
            for item in version_record.evaluators
            if "name" in item
        }
        if request.evaluator not in requirements:
            raise ValueError(f"evaluator is not required by this version: {request.evaluator}")
        passed = request.score >= requirements[request.evaluator]
        at = _aware(request.at or datetime.now(UTC))
        existing = session.scalars(
            sa.select(EvaluationRunRecord).where(
                EvaluationRunRecord.agent_id == agent_id,
                EvaluationRunRecord.version == request.version,
                EvaluationRunRecord.evaluator == request.evaluator,
                EvaluationRunRecord.run_id == request.run_id,
            )
        ).first()
        if existing is not None:
            stored = _run_from_row(existing)
            if stored.score != request.score or stored.details != request.details:
                raise ValueError(
                    "evaluation run already recorded with different results: "
                    f"{agent_id}:{request.version}:{request.evaluator}:{request.run_id}"
                )
            return stored
        row = EvaluationRunRecord(
            agent_id=agent_id,
            version=request.version,
            evaluator=request.evaluator,
            run_id=request.run_id,
            score=request.score,
            passed=passed,
            at=at,
            details=request.details,
        )
        session.add(row)
        session.flush()
        run = _run_from_row(row)
        audit.record(
            action="agent_evaluation.recorded",
            actor="evaluation-runner",
            subject=f"agent:{agent_id}:{request.version}",
            details={
                "evaluator": run.evaluator,
                "run_id": run.run_id,
                "score": run.score,
                "passed": run.passed,
            },
        )
        return run

    def evaluation_gate_in_transaction(
        self,
        session: Session,
        agent_id: str,
        version: str,
        now: datetime | None = None,
        recency_window_days: int = _DEFAULT_RECENCY_WINDOW_DAYS,
    ) -> bool:
        """True only when every required evaluator has a passing recent run."""
        version_record = session.get(AgentVersionRecord, {"agent_id": agent_id, "version": version})
        if version_record is None:
            raise ValueError(f"agent version not found: {agent_id}:{version}")
        required = {
            item["name"]: item["required_score"]
            for item in version_record.evaluators
            if "name" in item
        }
        if not required:
            return False
        current = _aware(now or datetime.now(UTC))
        cutoff = current - timedelta(days=recency_window_days)
        for evaluator in required:
            latest = session.scalars(
                sa.select(EvaluationRunRecord)
                .where(
                    EvaluationRunRecord.agent_id == agent_id,
                    EvaluationRunRecord.version == version,
                    EvaluationRunRecord.evaluator == evaluator,
                )
                .order_by(EvaluationRunRecord.at.desc(), EvaluationRunRecord.run_id.desc())
                .limit(1)
            ).first()
            if latest is None or latest.at is None:
                return False
            latest_at = _aware(latest.at)
            if not latest.passed or latest_at < cutoff:
                return False
        return True

    def evaluation_gate(
        self,
        agent_id: str,
        version: str,
        now: datetime | None = None,
        recency_window_days: int = _DEFAULT_RECENCY_WINDOW_DAYS,
    ) -> bool:
        with self._sessions() as session:
            return self.evaluation_gate_in_transaction(
                session, agent_id, version, now, recency_window_days
            )

    def list_evaluations(
        self, agent_id: str, version: str | None = None, limit: int = 50
    ) -> list[EvaluationRun]:
        limit = max(1, min(limit, _MAX_LIMIT))
        with self._sessions() as session:
            query = sa.select(EvaluationRunRecord).where(EvaluationRunRecord.agent_id == agent_id)
            if version is not None:
                query = query.where(EvaluationRunRecord.version == version)
            rows = session.scalars(
                query.order_by(
                    EvaluationRunRecord.at.desc(), EvaluationRunRecord.run_id.desc()
                ).limit(limit)
            ).all()
            return [_run_from_row(row) for row in rows]

    def record_execution_start(
        self,
        agent_id: str,
        execution_id: str,
        request: ExecutionAuditRecord,
    ) -> ExecutionAuditRecord:
        """Idempotently open an execution; matching re-start is a no-op."""
        if not _LOGICAL_ALIAS_PATTERN.fullmatch(request.model_alias):
            raise ValueError("model_alias must be a logical registry alias")
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(ExecutionAuditRecordModel).where(
                    ExecutionAuditRecordModel.agent_id == agent_id,
                    ExecutionAuditRecordModel.execution_id == execution_id,
                )
            ).first()
            if existing is not None:
                stored = self._execution_from_row(existing)
                if (
                    stored.version != request.version
                    or stored.model_alias != request.model_alias
                    or stored.action != request.action
                ):
                    raise ValueError(
                        "execution already recorded with different identity: "
                        f"{agent_id}:{execution_id}"
                    )
                return stored
            row = ExecutionAuditRecordModel(
                agent_id=agent_id,
                execution_id=execution_id,
                version=request.version,
                model_alias=request.model_alias,
                action=request.action,
                started_at=_aware(request.started_at),
                outcome=ExecutionOutcome.RUNNING.value,
                error_class=None,
            )
            session.add(row)
            session.flush()
            return self._execution_from_row(row)

    def record_execution_finish(
        self,
        agent_id: str,
        execution_id: str,
        outcome: ExecutionOutcome,
        finished_at: datetime | None = None,
        error_class: str | None = None,
    ) -> ExecutionAuditRecord:
        """Close an execution exactly once; a matching repeat is a no-op."""
        if outcome is ExecutionOutcome.RUNNING:
            raise ValueError("finished execution must be succeeded or failed")
        if outcome is ExecutionOutcome.FAILED and not error_class:
            raise ValueError("failed execution requires error_class")
        if outcome is ExecutionOutcome.SUCCEEDED and error_class is not None:
            raise ValueError("successful execution cannot have error_class")
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(ExecutionAuditRecordModel).where(
                    ExecutionAuditRecordModel.agent_id == agent_id,
                    ExecutionAuditRecordModel.execution_id == execution_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"execution audit not found: {agent_id}:{execution_id}")
            if row.finished_at is not None:
                stored = self._execution_from_row(row)
                if (
                    stored.outcome is outcome
                    and stored.error_class == error_class
                    and stored.finished_at is not None
                    and _aware(stored.finished_at) == _aware(finished_at or row.finished_at)
                ):
                    return stored
                raise ValueError(f"execution already finished: {agent_id}:{execution_id}")
            row.finished_at = _aware(finished_at or datetime.now(UTC))
            row.outcome = outcome.value
            row.error_class = error_class
            session.flush()
            return self._execution_from_row(row)

    @staticmethod
    def _execution_from_row(row: ExecutionAuditRecordModel) -> ExecutionAuditRecord:
        return ExecutionAuditRecord(
            agent_id=row.agent_id,
            execution_id=row.execution_id,
            version=row.version,
            model_alias=row.model_alias,
            action=row.action,
            started_at=_aware(row.started_at),
            finished_at=_aware(row.finished_at) if row.finished_at is not None else None,
            outcome=ExecutionOutcome(row.outcome),
            error_class=row.error_class,
        )

    @contextmanager
    def execute_audited(
        self,
        agent_id: str,
        execution_id: str,
        version: str,
        model_alias: str,
        action: str,
        started_at: datetime | None = None,
    ) -> Iterator[ExecutionAuditRecord]:
        """Run an execution with failure-safe audit: exceptions finish as failed."""
        record = self.record_execution_start(
            agent_id,
            execution_id,
            ExecutionAuditRecord(
                agent_id=agent_id,
                execution_id=execution_id,
                version=version,
                model_alias=model_alias,
                action=action,
                started_at=started_at or datetime.now(UTC),
            ),
        )
        try:
            yield record
        except BaseException as exc:
            self.record_execution_finish(
                agent_id,
                execution_id,
                ExecutionOutcome.FAILED,
                error_class=type(exc).__name__,
            )
            raise
        self.record_execution_finish(agent_id, execution_id, ExecutionOutcome.SUCCEEDED)

    def list_executions(self, agent_id: str, limit: int = 20) -> list[ExecutionAuditRecord]:
        limit = max(1, min(limit, _MAX_LIMIT))
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ExecutionAuditRecordModel)
                .where(ExecutionAuditRecordModel.agent_id == agent_id)
                .order_by(
                    ExecutionAuditRecordModel.started_at.desc(),
                    ExecutionAuditRecordModel.execution_id,
                )
                .limit(limit)
            ).all()
            return [self._execution_from_row(row) for row in rows]


def _evaluation_store(engine: Annotated[Engine, Depends(database_engine)]) -> AgentEvaluationStore:
    return AgentEvaluationStore.from_engine(engine)


def _agent_or_404(agent_id: str, store: AgentEvaluationStore) -> None:
    with store._sessions() as session:
        if (
            session.scalars(
                sa.select(AgentVersionRecord.agent_id).where(
                    AgentVersionRecord.agent_id == agent_id
                )
            ).first()
            is None
        ):
            raise HTTPException(status_code=404, detail="agent not found")


def record_evaluation_route(
    agent_id: str,
    request: RecordEvaluationRequest,
    store: Annotated[AgentEvaluationStore, Depends(_evaluation_store)],
) -> EvaluationRun:
    try:
        return store.record_evaluation(agent_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def get_evaluations_route(
    agent_id: str,
    store: Annotated[AgentEvaluationStore, Depends(_evaluation_store)],
    version: str | None = None,
    limit: int = 50,
) -> list[EvaluationRun]:
    _agent_or_404(agent_id, store)
    return store.list_evaluations(agent_id, version, limit)


def get_executions_route(
    agent_id: str,
    store: Annotated[AgentEvaluationStore, Depends(_evaluation_store)],
    limit: int = 20,
) -> list[ExecutionAuditRecord]:
    _agent_or_404(agent_id, store)
    return store.list_executions(agent_id, limit)


router = APIRouter(prefix="/agents", tags=["agent-evaluations"])
router.add_api_route(
    "/{agent_id}/evaluations",
    get_evaluations_route,
    methods=["GET"],
    name="get_agent_evaluations",
)
router.add_api_route(
    "/{agent_id}/evaluations/record",
    record_evaluation_route,
    methods=["POST"],
    name="record_agent_evaluation",
)
router.add_api_route(
    "/{agent_id}/executions",
    get_executions_route,
    methods=["GET"],
    name="get_agent_executions",
)
