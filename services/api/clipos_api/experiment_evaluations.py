"""Result evaluation and business significance (CLIP-068).

Closes the M16 analysis loop over CLIP-067 variant assignments (PRD §H
experiments, journey 4 "economics calculated"):

- **Pure significance math** — `evaluate_significance` compares two
  variant samples (n, mean, stddev) with Welch's t-test in its large-n
  normal approximation: se = sqrt(sd_a^2/n_a + sd_b^2/n_b),
  z = (mean_b - mean_a)/se, two-sided p = erfc(|z|/sqrt(2)). Statistical
  significance is |z| >= z_critical (default 1.96 ~ 95%). Business
  significance additionally requires |lift_pct| >= min_effect_pct
  (default 5.0) — an effect can be real but too small to matter.
  `lift_pct` is variant B relative to A. Degenerate inputs (se = 0 with
  a mean difference) are treated as infinitely significant; identical
  means yield z = 0, p = 1. No LLM, no network, no scipy.
- **Persistence** — `experiment_evaluations` (migration 0046) stores
  every evaluation, idempotent per (org_id, eval_id) with slug ids and
  conflict on changed fields (409). Winner is the variant that beats
  the other by the business threshold under statistical significance;
  otherwise None (inconclusive).
- **Routes** (CLIP-033 pattern): POST
  /experiments/{experiment_id}/evaluate (experiment must exist in the
  org, 404), GET /experiments/{experiment_id}/evaluations.

Audits: `experiment_evaluations.recorded`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from math import erfc, sqrt
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN, ExperimentRecord
from clipos_api.repository import Base

DEFAULT_Z_CRITICAL = 1.96
DEFAULT_MIN_EFFECT_PCT = 5.0


class VariantStats(BaseModel):
    """Summary statistics for one variant's metric observations."""

    model_config = ConfigDict(frozen=True)

    variant_key: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    sample_count: int = Field(ge=2, le=1_000_000_000)
    mean: float = Field(ge=-1e12, le=1e12)
    stddev: float = Field(ge=0.0, le=1e12)


class SignificanceResult(BaseModel):
    """Pure output of the two-sample comparison."""

    model_config = ConfigDict(frozen=True)

    lift_pct: float
    z_score: float
    p_value: float
    statistically_significant: bool
    business_significant: bool
    winner_variant_key: str | None = Field(default=None, max_length=64)


class EvaluateRequest(BaseModel):
    """Request body for evaluating one A/B comparison."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    eval_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    variant_a: VariantStats
    variant_b: VariantStats
    z_critical: float = Field(default=DEFAULT_Z_CRITICAL, gt=0.0, le=10.0)
    min_effect_pct: float = Field(default=DEFAULT_MIN_EFFECT_PCT, ge=0.0, le=1000.0)
    actor_ref: str = Field(min_length=1, max_length=255)


class ExperimentEvaluation(BaseModel):
    """One stored evaluation of an A/B comparison."""

    model_config = ConfigDict(frozen=True)

    eval_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    experiment_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    variant_a: VariantStats
    variant_b: VariantStats
    z_critical: float
    min_effect_pct: float
    lift_pct: float
    z_score: float
    p_value: float
    statistically_significant: bool
    business_significant: bool
    winner_variant_key: str | None = Field(default=None, max_length=64)
    actor_ref: str = Field(min_length=1, max_length=255)
    created_at: datetime | None = None


def evaluate_significance(
    variant_a: VariantStats,
    variant_b: VariantStats,
    z_critical: float = DEFAULT_Z_CRITICAL,
    min_effect_pct: float = DEFAULT_MIN_EFFECT_PCT,
) -> SignificanceResult:
    """Pure Welch two-sample comparison (normal approximation).

    lift_pct is variant B relative to A. Statistical significance uses
    |z| >= z_critical; business significance also requires the effect to
    clear min_effect_pct. Zero standard errors with different means are
    infinitely significant; equal means are never significant.
    """
    if variant_a.mean == variant_b.mean:
        return SignificanceResult(
            lift_pct=0.0,
            z_score=0.0,
            p_value=1.0,
            statistically_significant=False,
            business_significant=False,
            winner_variant_key=None,
        )
    lift_pct = (variant_b.mean - variant_a.mean) / max(abs(variant_a.mean), 1e-12) * 100.0
    variance_a = variant_a.stddev**2 / variant_a.sample_count
    variance_b = variant_b.stddev**2 / variant_b.sample_count
    se = sqrt(variance_a + variance_b)
    if se == 0.0:
        z_score = float("inf") if variant_b.mean > variant_a.mean else float("-inf")
        p_value = 0.0
    else:
        z_score = (variant_b.mean - variant_a.mean) / se
        p_value = erfc(abs(z_score) / sqrt(2.0))
    statistically_significant = abs(z_score) >= z_critical
    business_significant = statistically_significant and abs(lift_pct) >= min_effect_pct
    winner: str | None = None
    if business_significant:
        winner = variant_b.variant_key if lift_pct > 0 else variant_a.variant_key
    return SignificanceResult(
        lift_pct=lift_pct,
        z_score=z_score,
        p_value=p_value,
        statistically_significant=statistically_significant,
        business_significant=business_significant,
        winner_variant_key=winner,
    )


class ExperimentEvaluationRecord(Base):
    """SQL row for one stored evaluation."""

    __tablename__ = "experiment_evaluations"
    __table_args__ = (
        sa.Index(
            "uq_experiment_evaluations_identity",
            "org_id",
            "eval_id",
            unique=True,
        ),
        sa.Index("ix_experiment_evaluations_org_experiment", "org_id", "experiment_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    eval_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    variant_a_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    variant_a_sample: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    variant_a_mean: Mapped[float] = mapped_column(sa.Float, nullable=False)
    variant_a_stddev: Mapped[float] = mapped_column(sa.Float, nullable=False)
    variant_b_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    variant_b_sample: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    variant_b_mean: Mapped[float] = mapped_column(sa.Float, nullable=False)
    variant_b_stddev: Mapped[float] = mapped_column(sa.Float, nullable=False)
    z_critical: Mapped[float] = mapped_column(sa.Float, nullable=False)
    min_effect_pct: Mapped[float] = mapped_column(sa.Float, nullable=False)
    lift_pct: Mapped[float] = mapped_column(sa.Float, nullable=False)
    z_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    p_value: Mapped[float] = mapped_column(sa.Float, nullable=False)
    statistically_significant: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    business_significant: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    winner_variant_key: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    actor_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _evaluation_from_row(row: ExperimentEvaluationRecord) -> ExperimentEvaluation:
    return ExperimentEvaluation(
        eval_id=row.eval_id,
        org_id=row.org_id,
        experiment_id=row.experiment_id,
        variant_a=VariantStats(
            variant_key=row.variant_a_key,
            sample_count=row.variant_a_sample,
            mean=row.variant_a_mean,
            stddev=row.variant_a_stddev,
        ),
        variant_b=VariantStats(
            variant_key=row.variant_b_key,
            sample_count=row.variant_b_sample,
            mean=row.variant_b_mean,
            stddev=row.variant_b_stddev,
        ),
        z_critical=row.z_critical,
        min_effect_pct=row.min_effect_pct,
        lift_pct=row.lift_pct,
        z_score=row.z_score,
        p_value=row.p_value,
        statistically_significant=row.statistically_significant,
        business_significant=row.business_significant,
        winner_variant_key=row.winner_variant_key,
        actor_ref=row.actor_ref,
        created_at=_aware(row.created_at),
    )


class EvaluationStore:
    """Typed persistence for experiment evaluations."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> EvaluationStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def evaluate(self, experiment_id: str, request: EvaluateRequest) -> ExperimentEvaluation:
        """Compute significance and persist; experiment must exist (404).

        Idempotent per (org_id, eval_id): a repeat with identical inputs
        returns the stored row; changed inputs conflict (409).
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            experiment = session.scalars(
                sa.select(ExperimentRecord).where(
                    ExperimentRecord.org_id == request.org_id,
                    ExperimentRecord.experiment_id == experiment_id,
                )
            ).first()
            if experiment is None:
                raise ValueError(f"experiment not found: {experiment_id}")
            existing = session.scalars(
                sa.select(ExperimentEvaluationRecord).where(
                    ExperimentEvaluationRecord.org_id == request.org_id,
                    ExperimentEvaluationRecord.eval_id == request.eval_id,
                )
            ).first()
            if existing is not None:
                stored = _evaluation_from_row(existing)
                if (
                    stored.variant_a != request.variant_a
                    or stored.variant_b != request.variant_b
                    or stored.z_critical != request.z_critical
                    or stored.min_effect_pct != request.min_effect_pct
                ):
                    raise ValueError(
                        f"evaluation already recorded with different fields: {request.eval_id}"
                    )
                return stored
            significance = evaluate_significance(
                request.variant_a,
                request.variant_b,
                request.z_critical,
                request.min_effect_pct,
            )
            row = ExperimentEvaluationRecord(
                eval_id=request.eval_id,
                org_id=request.org_id,
                experiment_id=experiment_id,
                variant_a_key=request.variant_a.variant_key,
                variant_a_sample=request.variant_a.sample_count,
                variant_a_mean=request.variant_a.mean,
                variant_a_stddev=request.variant_a.stddev,
                variant_b_key=request.variant_b.variant_key,
                variant_b_sample=request.variant_b.sample_count,
                variant_b_mean=request.variant_b.mean,
                variant_b_stddev=request.variant_b.stddev,
                z_critical=request.z_critical,
                min_effect_pct=request.min_effect_pct,
                lift_pct=significance.lift_pct,
                z_score=significance.z_score,
                p_value=significance.p_value,
                statistically_significant=significance.statistically_significant,
                business_significant=significance.business_significant,
                winner_variant_key=significance.winner_variant_key,
                actor_ref=request.actor_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "experiment_evaluations.recorded",
                "experiment_evaluations",
                f"experiment_evaluation:{request.eval_id}",
                {
                    "org_id": request.org_id,
                    "experiment_id": experiment_id,
                    "lift_pct": significance.lift_pct,
                    "business_significant": significance.business_significant,
                    "winner_variant_key": significance.winner_variant_key,
                },
            )
            return _evaluation_from_row(row)

    def list_evaluations(self, org_id: str, experiment_id: str) -> list[ExperimentEvaluation]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ExperimentEvaluationRecord)
                .where(
                    ExperimentEvaluationRecord.org_id == org_id,
                    ExperimentEvaluationRecord.experiment_id == experiment_id,
                )
                .order_by(ExperimentEvaluationRecord.created_at)
            ).all()
            return [_evaluation_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["experiments"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> EvaluationStore:
    return EvaluationStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already recorded" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post(
    "/experiments/{experiment_id}/evaluate",
    response_model=ExperimentEvaluation,
    name="evaluate_experiment",
)
def evaluate_experiment_route(
    experiment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: EvaluateRequest,
    store: Annotated[EvaluationStore, Depends(_store)],
) -> ExperimentEvaluation:
    """Evaluate one A/B comparison and persist the result."""
    try:
        return store.evaluate(experiment_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/experiments/{experiment_id}/evaluations",
    response_model=list[ExperimentEvaluation],
    name="list_experiment_evaluations",
)
def list_experiment_evaluations_route(
    experiment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[EvaluationStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[ExperimentEvaluation]:
    """List stored evaluations for one experiment."""
    return store.list_evaluations(org_id, experiment_id)
