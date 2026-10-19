"""SLO objectives and operations qualification (CLIP-100).

M24 is *Autonomous Operations Qualification*: end-to-end production
qualification, guarded public mode, **SLOs**, operator runbook, autonomy
evidence. The runbook's daily-checks section already enumerates the
unattended controls (budget utilization, failed workflows, dead letters,
backup freshness) and the M24 exit gate asks for SLOs alongside them.

The other pieces already exist as their own modules — `budgets` (cost caps),
`publishing`/`remediation` (kill switches), `incidents` (incident lifecycle
and runbooks), `monitors`, `backup_verification` (restore drills). What was
missing was (a) any notion of an SLO at all, and (b) one operator-visible
place that says whether the operational controls actually *work*.

- **SLO objectives** — `slo_objectives` are org-scoped, idempotent per
  (org_id, objective_id). Each names a generic service indicator slug, a
  target in basis points (integer, never a float), a window in days and a
  minimum sample count. Targets out of 0..10000 are rejected, so an
  objective can never be defined that is unsatisfiable or vacuous by typo.
  Audited `slo_objectives.registered`.
- **Pure error-budget math** — :func:`evaluate_slo` computes attainment in
  basis points from good/total observations and reports the remaining error
  budget. Two distinct failure modes are kept apart deliberately, exactly as
  the backup restore evaluation does: **breached** (measured attainment
  below target) versus **insufficient data** (fewer observations than the
  objective's minimum). Reporting "unknown" as "healthy" is the failure this
  design exists to prevent.
- **Drill records** — `ops_drills` are append-only and idempotent per
  (org_id, drill_id); each records one exercised operational control
  (a kill switch closed, a cost cap refused, an incident drill run) with its
  outcome and evidence reference. Audited `ops_drills.recorded`.
- **Qualification verdict** — :func:`qualify_operations` folds objectives and
  drills into one PASS/FAIL verdict with an explicit list of what is
  unverified, so "we did not test it" is never rendered as "it works".

Routes (CLIP-033 pattern): POST/GET /slo/objectives,
POST /slo/objectives/{objective_id}/evaluate, POST/GET /slo/drills,
GET /slo/qualification. No LLM, no network — callers supply measurements
and drill observations from their own runners.
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
from clipos_api.repository import Base

BASIS_POINTS = 10000
DEFAULT_MIN_SAMPLES = 1

# The operational controls M24 must have exercised before v1 freezes.
REQUIRED_CONTROLS = (
    "kill_switch_closed",
    "cost_cap_refused",
    "incident_drill",
)


class SloStatus(str, Enum):
    """Outcome of one SLO evaluation."""

    MET = "MET"
    BREACHED = "BREACHED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class DrillOutcome(str, Enum):
    """Outcome of one operational drill."""

    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class SloEvaluation(BaseModel):
    """Pure SLO verdict."""

    model_config = ConfigDict(frozen=True)

    objective_id: str
    status: SloStatus
    target_bps: int
    attainment_bps: int
    good: int
    total: int
    error_budget_remaining_bps: int
    detail: str


class SloObjective(BaseModel):
    """One declared objective."""

    model_config = ConfigDict(from_attributes=True)

    objective_id: str
    org_id: str
    indicator: str
    target_bps: int
    window_days: int
    min_samples: int
    created_at: datetime


class OpsDrill(BaseModel):
    """One recorded operational drill."""

    model_config = ConfigDict(from_attributes=True)

    drill_id: str
    org_id: str
    control: str
    outcome: DrillOutcome
    evidence_ref: str
    detail: str
    recorded_at: datetime


class QualificationResult(BaseModel):
    """Aggregate operational qualification."""

    model_config = ConfigDict(frozen=True)

    org_id: str
    verdict: str
    objectives_total: int
    objectives_met: int
    objectives_breached: int
    objectives_insufficient: int
    controls_verified: list[str] = Field(default_factory=list)
    controls_unverified: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)


class RegisterObjectiveRequest(BaseModel):
    """Declare one SLO objective."""

    objective_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    indicator: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    target_bps: int = Field(ge=0, le=BASIS_POINTS)
    window_days: int = Field(default=30, ge=1, le=365)
    min_samples: int = Field(default=DEFAULT_MIN_SAMPLES, ge=1)


class EvaluateSloRequest(BaseModel):
    """Measure one objective against observed counts."""

    org_id: str = Field(min_length=1, max_length=64)
    good: int = Field(ge=0)
    total: int = Field(ge=0)


class RecordDrillRequest(BaseModel):
    """Record one exercised operational control."""

    drill_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    control: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    outcome: DrillOutcome
    evidence_ref: str = Field(min_length=1, max_length=512)
    detail: str = Field(default="", max_length=1000)


class SloObjectiveRecord(Base):
    """SQL row for one SLO objective."""

    __tablename__ = "slo_objectives"
    __table_args__ = (
        sa.Index("uq_slo_objectives_org_objective", "org_id", "objective_id", unique=True),
        sa.Index("ix_slo_objectives_org_indicator", "org_id", "indicator"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    objective_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    indicator: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    target_bps: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    window_days: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    min_samples: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class OpsDrillRecord(Base):
    """SQL row for one operational drill (append-only)."""

    __tablename__ = "ops_drills"
    __table_args__ = (
        sa.Index("uq_ops_drills_org_drill", "org_id", "drill_id", unique=True),
        sa.Index("ix_ops_drills_org_control", "org_id", "control"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    drill_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    control: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    detail: Mapped[str] = mapped_column(sa.String(1000), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def attainment_bps(good: int, total: int) -> int:
    """Pure: good/total as basis points, integer arithmetic only."""
    if total <= 0:
        return 0
    return (good * BASIS_POINTS) // total


def evaluate_slo(
    objective_id: str,
    target_bps: int,
    good: int,
    total: int,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> SloEvaluation:
    """Pure: does the objective hold?

    Two failure modes stay distinct on purpose. Insufficient data is NOT a
    pass: reporting "we did not measure enough to know" as healthy is exactly
    the dishonesty this gate exists to prevent.
    """
    if total < min_samples:
        return SloEvaluation(
            objective_id=objective_id,
            status=SloStatus.INSUFFICIENT_DATA,
            target_bps=target_bps,
            attainment_bps=attainment_bps(good, total),
            good=good,
            total=total,
            error_budget_remaining_bps=0,
            detail=(
                f"only {total} observation(s); the objective requires at least "
                f"{min_samples} before it can be judged"
            ),
        )

    measured = attainment_bps(good, total)
    remaining = max(0, measured - target_bps)
    if measured >= target_bps:
        return SloEvaluation(
            objective_id=objective_id,
            status=SloStatus.MET,
            target_bps=target_bps,
            attainment_bps=measured,
            good=good,
            total=total,
            error_budget_remaining_bps=remaining,
            detail=f"attainment {measured}bps >= target {target_bps}bps",
        )
    return SloEvaluation(
        objective_id=objective_id,
        status=SloStatus.BREACHED,
        target_bps=target_bps,
        attainment_bps=measured,
        good=good,
        total=total,
        error_budget_remaining_bps=0,
        detail=f"attainment {measured}bps below target {target_bps}bps",
    )


def verify_cost_cap(
    authorize_spend, cap_cents: int, spent_cents: int, requested_cents: int
) -> tuple[bool, str]:
    """Pure (via the injected authorizer): does the cost cap actually refuse?

    Takes the budget authorizer as an argument so this module never imports
    the budgets module at import time and the check is trivially testable.
    A cap that does not refuse an over-cap request is the failure.
    """
    decision = authorize_spend(cap_cents, spent_cents, requested_cents)
    allowed = bool(decision.allowed)
    if requested_cents > 0 and spent_cents + requested_cents > cap_cents:
        if allowed:
            return False, "authorizer allowed spend beyond the cap"
        return True, f"refused with reason {decision.reason}"
    if not allowed:
        return False, f"authorizer refused a within-cap request ({decision.reason})"
    return True, "within-cap request allowed"


def qualify_operations(
    org_id: str,
    evaluations: list[SloEvaluation],
    drills: list[OpsDrill],
    required_controls: tuple[str, ...] = REQUIRED_CONTROLS,
) -> QualificationResult:
    """Pure: fold objectives and drills into one verdict.

    ``controls_unverified`` names every required control with no VERIFIED
    drill, so an untested control is reported as unverified rather than
    silently assumed to work.
    """
    met = [e for e in evaluations if e.status is SloStatus.MET]
    breached = [e for e in evaluations if e.status is SloStatus.BREACHED]
    insufficient = [e for e in evaluations if e.status is SloStatus.INSUFFICIENT_DATA]

    verified = sorted({d.control for d in drills if d.outcome is DrillOutcome.VERIFIED})
    failed = sorted({d.control for d in drills if d.outcome is DrillOutcome.FAILED})
    unverified = sorted(c for c in required_controls if c not in verified or c in failed)

    findings: list[str] = []
    for evaluation in breached:
        findings.append(f"SLO breached: {evaluation.objective_id} ({evaluation.detail})")
    for evaluation in insufficient:
        findings.append(f"SLO unmeasured: {evaluation.objective_id} ({evaluation.detail})")
    for control in unverified:
        if control in failed:
            findings.append(f"control drill failed: {control}")
        else:
            findings.append(f"control not exercised: {control}")

    verdict = "PASS" if not breached and not insufficient and not unverified else "FAIL"
    return QualificationResult(
        org_id=org_id,
        verdict=verdict,
        objectives_total=len(evaluations),
        objectives_met=len(met),
        objectives_breached=len(breached),
        objectives_insufficient=len(insufficient),
        controls_verified=verified,
        controls_unverified=unverified,
        findings=findings,
    )


def _objective_from_row(row: SloObjectiveRecord) -> SloObjective:
    return SloObjective(
        objective_id=row.objective_id,
        org_id=row.org_id,
        indicator=row.indicator,
        target_bps=row.target_bps,
        window_days=row.window_days,
        min_samples=row.min_samples,
        created_at=_aware(row.created_at),
    )


def _drill_from_row(row: OpsDrillRecord) -> OpsDrill:
    return OpsDrill(
        drill_id=row.drill_id,
        org_id=row.org_id,
        control=row.control,
        outcome=DrillOutcome(row.outcome),
        evidence_ref=row.evidence_ref,
        detail=row.detail,
        recorded_at=_aware(row.recorded_at),
    )


class SloStore:
    """Typed persistence for SLO objectives and operational drills."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> SloStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_objective(self, request: RegisterObjectiveRequest) -> SloObjective:
        """Idempotent per (org, objective_id)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(SloObjectiveRecord).where(
                    SloObjectiveRecord.org_id == request.org_id,
                    SloObjectiveRecord.objective_id == request.objective_id,
                )
            ).first()
            if existing is not None:
                stored = _objective_from_row(existing)
                if (
                    stored.indicator != request.indicator
                    or stored.target_bps != request.target_bps
                    or stored.window_days != request.window_days
                    or stored.min_samples != request.min_samples
                ):
                    raise ValueError(
                        f"objective already registered with different fields: "
                        f"{request.objective_id}"
                    )
                return stored

            row = SloObjectiveRecord(
                objective_id=request.objective_id,
                org_id=request.org_id,
                indicator=request.indicator,
                target_bps=request.target_bps,
                window_days=request.window_days,
                min_samples=request.min_samples,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "slo_objectives.registered",
                "slo-store",
                f"objective:{request.objective_id}",
                {
                    "org_id": request.org_id,
                    "indicator": request.indicator,
                    "target_bps": request.target_bps,
                    "window_days": request.window_days,
                },
            )
            return _objective_from_row(row)

    def get_objective(self, objective_id: str, org_id: str) -> SloObjective | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(SloObjectiveRecord).where(
                    SloObjectiveRecord.org_id == org_id,
                    SloObjectiveRecord.objective_id == objective_id,
                )
            ).first()
            return _objective_from_row(row) if row is not None else None

    def list_objectives(self, org_id: str) -> list[SloObjective]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(SloObjectiveRecord)
                .where(SloObjectiveRecord.org_id == org_id)
                .order_by(SloObjectiveRecord.id)
            ).all()
            return [_objective_from_row(row) for row in rows]

    def evaluate(self, objective_id: str, request: EvaluateSloRequest) -> SloEvaluation:
        """Measure one stored objective (404 when it does not exist)."""
        objective = self.get_objective(objective_id, request.org_id)
        if objective is None:
            raise ValueError(f"objective not found: {objective_id}")
        if request.good > request.total:
            raise ValueError(f"good observations exceed total: {request.good} > {request.total}")
        return evaluate_slo(
            objective.objective_id,
            objective.target_bps,
            request.good,
            request.total,
            objective.min_samples,
        )

    def record_drill(self, request: RecordDrillRequest) -> OpsDrill:
        """Append-only, idempotent per (org, drill_id)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(OpsDrillRecord).where(
                    OpsDrillRecord.org_id == request.org_id,
                    OpsDrillRecord.drill_id == request.drill_id,
                )
            ).first()
            if existing is not None:
                stored = _drill_from_row(existing)
                if stored.control != request.control or stored.outcome is not request.outcome:
                    raise ValueError(
                        f"drill already recorded with different fields: {request.drill_id}"
                    )
                return stored

            row = OpsDrillRecord(
                drill_id=request.drill_id,
                org_id=request.org_id,
                control=request.control,
                outcome=request.outcome.value,
                evidence_ref=request.evidence_ref,
                detail=request.detail,
                recorded_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "ops_drills.recorded",
                "slo-store",
                f"drill:{request.drill_id}",
                {
                    "org_id": request.org_id,
                    "control": request.control,
                    "outcome": request.outcome.value,
                    "evidence_ref": request.evidence_ref,
                },
            )
            return _drill_from_row(row)

    def list_drills(self, org_id: str) -> list[OpsDrill]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(OpsDrillRecord)
                .where(OpsDrillRecord.org_id == org_id)
                .order_by(OpsDrillRecord.id)
            ).all()
            return [_drill_from_row(row) for row in rows]

    def qualification(
        self,
        org_id: str,
        measurements: dict[str, tuple[int, int]] | None = None,
        required_controls: tuple[str, ...] = REQUIRED_CONTROLS,
    ) -> QualificationResult:
        """Qualify every objective the org declared plus its control drills."""
        measurements = measurements or {}
        evaluations: list[SloEvaluation] = []
        for objective in self.list_objectives(org_id):
            good, total = measurements.get(objective.objective_id, (0, 0))
            evaluations.append(
                evaluate_slo(
                    objective.objective_id,
                    objective.target_bps,
                    good,
                    total,
                    objective.min_samples,
                )
            )
        return qualify_operations(org_id, evaluations, self.list_drills(org_id), required_controls)


router = APIRouter(tags=["slo"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> SloStore:
    return SloStore.from_engine(engine)


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


@router.post("/slo/objectives", response_model=SloObjective, name="register_slo_objective")
def register_objective_route(
    request: RegisterObjectiveRequest,
    store: Annotated[SloStore, Depends(_store)],
) -> SloObjective:
    """Declare one SLO objective (idempotent)."""
    try:
        return store.register_objective(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/slo/objectives", response_model=list[SloObjective], name="list_slo_objectives")
def list_objectives_route(
    store: Annotated[SloStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[SloObjective]:
    """List SLO objectives for one org."""
    return store.list_objectives(org_id)


@router.get("/slo/objectives/{objective_id}", response_model=SloObjective, name="get_slo_objective")
def get_objective_route(
    objective_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[SloStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> SloObjective:
    """Get one SLO objective by id and org."""
    stored = store.get_objective(objective_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"objective not found: {objective_id}")
    return stored


@router.post(
    "/slo/objectives/{objective_id}/evaluate",
    response_model=SloEvaluation,
    name="evaluate_slo_objective",
)
def evaluate_objective_route(
    objective_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: EvaluateSloRequest,
    store: Annotated[SloStore, Depends(_store)],
) -> SloEvaluation:
    """Measure one objective against observed good/total counts."""
    try:
        return store.evaluate(objective_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post("/slo/drills", response_model=OpsDrill, name="record_ops_drill")
def record_drill_route(
    request: RecordDrillRequest,
    store: Annotated[SloStore, Depends(_store)],
) -> OpsDrill:
    """Record one exercised operational control (idempotent)."""
    try:
        return store.record_drill(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/slo/drills", response_model=list[OpsDrill], name="list_ops_drills")
def list_drills_route(
    store: Annotated[SloStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[OpsDrill]:
    """List operational drills for one org."""
    return store.list_drills(org_id)


@router.get("/slo/qualification", response_model=QualificationResult, name="slo_qualification")
def qualification_route(
    store: Annotated[SloStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> QualificationResult:
    """Operational qualification posture for one org."""
    return store.qualification(org_id)


__all__ = [
    "BASIS_POINTS",
    "DEFAULT_MIN_SAMPLES",
    "REQUIRED_CONTROLS",
    "DrillOutcome",
    "EvaluateSloRequest",
    "OpsDrill",
    "QualificationResult",
    "RecordDrillRequest",
    "RegisterObjectiveRequest",
    "SloEvaluation",
    "SloObjective",
    "SloStatus",
    "SloStore",
    "attainment_bps",
    "evaluate_slo",
    "qualify_operations",
    "router",
    "verify_cost_cap",
]
