"""CI/CD canary rollout and rollback (CLIP-095).

Deployment specification §Deployment / §Rollback / §Release evidence:

    `main -> build immutable images -> scan -> deploy staging -> migrate
     -> smoke/e2e/private publication test -> approval policy ->
     production canary -> health window -> full rollout`

This module makes that pipeline a first-class, auditable record rather
than an operator's memory.

- **Deployments** — `deployments` rows are org-scoped with client slug
  ids, idempotent per (org_id, deployment_id). Each records the commit
  SHA, the immutable image digest, the environment, the migration set
  applied, and the strategy (CANARY or FULL). Audited
  `deployments.registered`.
- **Rollout** — a pure transition table over
  ``PENDING -> CANARY -> PROMOTED`` with ``FAILED`` reachable from
  PENDING/CANARY, and ``ROLLED_BACK`` reachable from CANARY/PROMOTED
  (and, for a failed rollout, from FAILED). Same-state is a no-op;
  anything else is 409. Promotion out of CANARY requires a *passing
  health window* (pure predicate over the recorded observations), so a
  canary cannot be promoted on hope. Audited
  `deployments.status_changed`.
- **Rollback** — pure eligibility: a deployment may roll back only to a
  revision the org has actually recorded, and only when that target's
  migration set is a *subset* of the currently applied one. That is the
  expand/contract discipline from the spec: rolling back onto a schema
  that is missing migrations this release added would break the older
  application, so it is refused with a 409 naming the offending
  migration. Backup restore is disaster recovery, not rollback, and is
  deliberately not modelled here. Audited `deployments.rolled_back`.
- **Release evidence** — `release_evidence` rows are append-only and
  idempotent per (org_id, deployment_id): commit SHA, image digest,
  migrations, config version, agent/policy versions, CI run refs,
  security scan summary, deploy/rollback timestamps, rollback target and
  operator approval refs — the exact list §Release evidence requires.
  Audited `release_evidence.recorded`.

Migration 0069 (CREATE-only). Routes (CLIP-033 pattern): POST/GET
/deployments, GET /deployments/{deployment_id}, POST
/deployments/{deployment_id}/canary, /promote, /rollback,
POST/GET /deployments/{deployment_id}/evidence. No LLM, no network.
"""

from __future__ import annotations

import json
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


class Strategy(str, Enum):
    """Rollout strategy (spec §Deployment)."""

    CANARY = "CANARY"
    FULL = "FULL"


class DeploymentStatus(str, Enum):
    """Rollout lifecycle."""

    PENDING = "PENDING"
    CANARY = "CANARY"
    PROMOTED = "PROMOTED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class Environment(str, Enum):
    """Target environment (spec §Environments)."""

    STAGING = "STAGING"
    PRODUCTION = "PRODUCTION"


DEPLOYMENT_TRANSITIONS: dict[DeploymentStatus, frozenset[DeploymentStatus]] = {
    DeploymentStatus.PENDING: frozenset(
        {DeploymentStatus.CANARY, DeploymentStatus.PROMOTED, DeploymentStatus.FAILED}
    ),
    DeploymentStatus.CANARY: frozenset(
        {
            DeploymentStatus.PROMOTED,
            DeploymentStatus.FAILED,
            DeploymentStatus.ROLLED_BACK,
        }
    ),
    DeploymentStatus.PROMOTED: frozenset({DeploymentStatus.ROLLED_BACK}),
    DeploymentStatus.FAILED: frozenset({DeploymentStatus.ROLLED_BACK}),
    DeploymentStatus.ROLLED_BACK: frozenset(),
}


def deployment_transition_allowed(current: DeploymentStatus, target: DeploymentStatus) -> bool:
    """Pure: legal rollout move (same-state counts as no-op)."""
    if current is target:
        return True
    return target in DEPLOYMENT_TRANSITIONS[current]


def health_window_passed(observations: list[str]) -> bool:
    """Pure: the canary health window gate.

    Promotion out of CANARY requires a non-empty window in which every
    observation is HEALTHY. An empty window (nothing observed yet) does
    not pass — a canary is promoted on evidence, never on silence.
    """
    if not observations:
        return False
    return all(observation == "HEALTHY" for observation in observations)


def rollback_conflicts(current_migrations: list[str], target_migrations: list[str]) -> list[str]:
    """Pure: migrations the target lacks that the current release applied.

    Rolling back onto a schema missing these would break the older
    application, so a non-empty result means rollback must be refused
    (expand/contract discipline, spec §Rollback).
    """
    target_set = set(target_migrations)
    return sorted({m for m in current_migrations if m not in target_set})


class ReleaseEvidence(BaseModel):
    """The §Release evidence record for one deployment."""

    model_config = ConfigDict(from_attributes=True)

    deployment_id: str
    org_id: str
    commit_sha: str
    image_digest: str
    migrations: list[str] = Field(default_factory=list)
    config_version: str
    agent_versions: list[str] = Field(default_factory=list)
    policy_versions: list[str] = Field(default_factory=list)
    ci_runs: list[str] = Field(default_factory=list)
    security_scan_summary: str
    deployed_at: datetime | None = None
    rolled_back_at: datetime | None = None
    rollback_target: str | None = None
    operator_approvals: list[str] = Field(default_factory=list)


class Deployment(BaseModel):
    """One recorded deployment and its rollout state."""

    model_config = ConfigDict(from_attributes=True)

    deployment_id: str
    org_id: str
    environment: Environment
    strategy: Strategy
    status: DeploymentStatus
    commit_sha: str
    image_digest: str
    migrations: list[str] = Field(default_factory=list)
    parent_deployment_id: str | None = None
    health_observations: list[str] = Field(default_factory=list)
    created_at: datetime


class RegisterDeploymentRequest(BaseModel):
    """Register one immutable deployment."""

    deployment_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    environment: Environment
    strategy: Strategy
    commit_sha: str = Field(min_length=7, max_length=64)
    image_digest: str = Field(min_length=1, max_length=200)
    migrations: list[str] = Field(default_factory=list)
    parent_deployment_id: str | None = Field(default=None, max_length=128)


class CanaryRequest(BaseModel):
    """Start (or record) the canary stage."""

    org_id: str = Field(min_length=1, max_length=64)
    actor_ref: str = Field(min_length=1, max_length=255)


class PromoteRequest(BaseModel):
    """Promote a canary to full rollout after a passing health window."""

    org_id: str = Field(min_length=1, max_length=64)
    actor_ref: str = Field(min_length=1, max_length=255)
    health_observations: list[str] = Field(default_factory=list)


class RollbackRequest(BaseModel):
    """Roll a deployment back to a previously recorded revision."""

    org_id: str = Field(min_length=1, max_length=64)
    actor_ref: str = Field(min_length=1, max_length=255)
    target_deployment_id: str = Field(min_length=1, max_length=128)
    reason_ref: str = Field(min_length=1, max_length=512)


class RecordEvidenceRequest(BaseModel):
    """Record the §Release evidence for a deployment."""

    org_id: str = Field(min_length=1, max_length=64)
    actor_ref: str = Field(min_length=1, max_length=255)
    commit_sha: str = Field(min_length=7, max_length=64)
    image_digest: str = Field(min_length=1, max_length=200)
    migrations: list[str] = Field(default_factory=list)
    config_version: str = Field(min_length=1, max_length=128)
    agent_versions: list[str] = Field(default_factory=list)
    policy_versions: list[str] = Field(default_factory=list)
    ci_runs: list[str] = Field(default_factory=list)
    security_scan_summary: str = Field(min_length=1, max_length=2000)
    rollback_target: str | None = Field(default=None, max_length=128)
    operator_approvals: list[str] = Field(default_factory=list)


class DeploymentRecord(Base):
    """SQL row for one deployment."""

    __tablename__ = "deployments"
    __table_args__ = (
        sa.Index(
            "uq_deployments_org_deployment",
            "org_id",
            "deployment_id",
            unique=True,
        ),
        sa.Index("ix_deployments_org_status", "org_id", "status"),
        sa.Index("ix_deployments_org_environment", "org_id", "environment"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    environment: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    strategy: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    commit_sha: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    image_digest: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    migrations_json: Mapped[str] = mapped_column(sa.String(4000), nullable=False, default="[]")
    parent_deployment_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    health_json: Mapped[str] = mapped_column(sa.String(4000), nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ReleaseEvidenceRecord(Base):
    """SQL row for one release-evidence record (append-only)."""

    __tablename__ = "release_evidence"
    __table_args__ = (
        sa.Index(
            "uq_release_evidence_org_deployment",
            "org_id",
            "deployment_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    commit_sha: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    image_digest: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    migrations_json: Mapped[str] = mapped_column(sa.String(4000), nullable=False, default="[]")
    config_version: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    agent_versions_json: Mapped[str] = mapped_column(sa.String(4000), nullable=False, default="[]")
    policy_versions_json: Mapped[str] = mapped_column(sa.String(4000), nullable=False, default="[]")
    ci_runs_json: Mapped[str] = mapped_column(sa.String(4000), nullable=False, default="[]")
    security_scan_summary: Mapped[str] = mapped_column(sa.String(2000), nullable=False)
    deployed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    rollback_target: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    operator_approvals_json: Mapped[str] = mapped_column(
        sa.String(4000), nullable=False, default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _load_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [str(item) for item in json.loads(raw)]


def _dump_list(values: list[str]) -> str:
    return json.dumps(list(values))


def _deployment_from_row(row: DeploymentRecord) -> Deployment:
    return Deployment(
        deployment_id=row.deployment_id,
        org_id=row.org_id,
        environment=Environment(row.environment),
        strategy=Strategy(row.strategy),
        status=DeploymentStatus(row.status),
        commit_sha=row.commit_sha,
        image_digest=row.image_digest,
        migrations=_load_list(row.migrations_json),
        parent_deployment_id=row.parent_deployment_id,
        health_observations=_load_list(row.health_json),
        created_at=_aware(row.created_at),
    )


def _evidence_from_row(row: ReleaseEvidenceRecord) -> ReleaseEvidence:
    return ReleaseEvidence(
        deployment_id=row.deployment_id,
        org_id=row.org_id,
        commit_sha=row.commit_sha,
        image_digest=row.image_digest,
        migrations=_load_list(row.migrations_json),
        config_version=row.config_version,
        agent_versions=_load_list(row.agent_versions_json),
        policy_versions=_load_list(row.policy_versions_json),
        ci_runs=_load_list(row.ci_runs_json),
        security_scan_summary=row.security_scan_summary,
        deployed_at=_aware(row.deployed_at) if row.deployed_at else None,
        rolled_back_at=_aware(row.rolled_back_at) if row.rolled_back_at else None,
        rollback_target=row.rollback_target,
        operator_approvals=_load_list(row.operator_approvals_json),
    )


class DeploymentStore:
    """Typed persistence for deployments and release evidence."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> DeploymentStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def _get_row(self, session: Session, deployment_id: str, org_id: str) -> DeploymentRecord:
        row = session.scalars(
            sa.select(DeploymentRecord).where(
                DeploymentRecord.org_id == org_id,
                DeploymentRecord.deployment_id == deployment_id,
            )
        ).first()
        if row is None:
            raise ValueError(f"deployment not found: {deployment_id}")
        return row

    # -- registration ------------------------------------------------------

    def register_deployment(self, request: RegisterDeploymentRequest) -> Deployment:
        """Idempotent per (org, deployment_id)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(DeploymentRecord).where(
                    DeploymentRecord.org_id == request.org_id,
                    DeploymentRecord.deployment_id == request.deployment_id,
                )
            ).first()
            if existing is not None:
                stored = _deployment_from_row(existing)
                if (
                    stored.commit_sha != request.commit_sha
                    or stored.image_digest != request.image_digest
                    or stored.environment is not request.environment
                    or stored.strategy is not request.strategy
                    or stored.migrations != list(request.migrations)
                ):
                    raise ValueError(
                        f"deployment already registered with different fields: "
                        f"{request.deployment_id}"
                    )
                return stored

            if request.parent_deployment_id is not None:
                self._get_row(session, request.parent_deployment_id, request.org_id)

            # A FULL-strategy deployment starts as if it had already cleared
            # the canary; a CANARY-strategy one starts at CANARY directly,
            # since registering it *is* the canary step.
            initial = (
                DeploymentStatus.CANARY
                if request.strategy is Strategy.CANARY
                else DeploymentStatus.PROMOTED
            )
            row = DeploymentRecord(
                deployment_id=request.deployment_id,
                org_id=request.org_id,
                environment=request.environment.value,
                strategy=request.strategy.value,
                status=initial.value,
                commit_sha=request.commit_sha,
                image_digest=request.image_digest,
                migrations_json=_dump_list(list(request.migrations)),
                parent_deployment_id=request.parent_deployment_id,
                health_json=_dump_list([]),
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "deployments.registered",
                "deployment-store",
                f"deployment:{request.deployment_id}",
                {
                    "org_id": request.org_id,
                    "environment": request.environment.value,
                    "strategy": request.strategy.value,
                    "commit_sha": request.commit_sha,
                    "migration_count": len(request.migrations),
                },
            )
            return _deployment_from_row(row)

    def get_deployment(self, deployment_id: str, org_id: str) -> Deployment | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(DeploymentRecord).where(
                    DeploymentRecord.org_id == org_id,
                    DeploymentRecord.deployment_id == deployment_id,
                )
            ).first()
            return _deployment_from_row(row) if row is not None else None

    def list_deployments(
        self, org_id: str, environment: Environment | None = None
    ) -> list[Deployment]:
        with self._sessions() as session:
            statement = sa.select(DeploymentRecord).where(DeploymentRecord.org_id == org_id)
            if environment is not None:
                statement = statement.where(DeploymentRecord.environment == environment.value)
            rows = session.scalars(statement.order_by(DeploymentRecord.id)).all()
            return [_deployment_from_row(row) for row in rows]

    # -- rollout -----------------------------------------------------------

    def start_canary(self, deployment_id: str, request: CanaryRequest) -> Deployment:
        """Move PENDING -> CANARY (idempotent when already CANARY)."""
        return self._transition(
            deployment_id,
            request.org_id,
            DeploymentStatus.CANARY,
            "deployments.canary_started",
            {"actor_ref": request.actor_ref},
            request.actor_ref,
        )

    def promote(self, deployment_id: str, request: PromoteRequest) -> Deployment:
        """CANARY -> PROMOTED, gated on a passing health window."""
        with self._sessions.begin() as session:
            row = self._get_row(session, deployment_id, request.org_id)
            current = DeploymentStatus(row.status)
            if current is DeploymentStatus.PROMOTED:
                return _deployment_from_row(row)
            if not deployment_transition_allowed(current, DeploymentStatus.PROMOTED):
                raise ValueError(f"illegal deployment transition: {current.value} -> PROMOTED")
            observations = list(request.health_observations) or _load_list(row.health_json)
            if not health_window_passed(observations):
                raise ValueError(
                    f"canary health window has not passed: {deployment_id} "
                    f"(need non-empty all-HEALTHY observations, got {observations})"
                )
            row.status = DeploymentStatus.PROMOTED.value
            row.health_json = _dump_list(observations)
            session.flush()
            audit.record(
                "deployments.status_changed",
                "deployment-store",
                f"deployment:{deployment_id}",
                {
                    "org_id": request.org_id,
                    "from": current.value,
                    "to": DeploymentStatus.PROMOTED.value,
                    "actor_ref": request.actor_ref,
                    "health_observations": len(observations),
                },
            )
            return _deployment_from_row(row)

    def fail(self, deployment_id: str, request: CanaryRequest) -> Deployment:
        """CANARY/PENDING -> FAILED."""
        return self._transition(
            deployment_id,
            request.org_id,
            DeploymentStatus.FAILED,
            "deployments.status_changed",
            {"actor_ref": request.actor_ref},
            request.actor_ref,
        )

    def _transition(
        self,
        deployment_id: str,
        org_id: str,
        target: DeploymentStatus,
        action: str,
        extra: dict,
        actor_ref: str,
    ) -> Deployment:
        with self._sessions.begin() as session:
            row = self._get_row(session, deployment_id, org_id)
            current = DeploymentStatus(row.status)
            if current is target:
                return _deployment_from_row(row)
            if not deployment_transition_allowed(current, target):
                raise ValueError(
                    f"illegal deployment transition: {current.value} -> {target.value}"
                )
            row.status = target.value
            session.flush()
            audit.record(
                action,
                "deployment-store",
                f"deployment:{deployment_id}",
                {
                    "org_id": org_id,
                    "from": current.value,
                    "to": target.value,
                    **extra,
                },
            )
            return _deployment_from_row(row)

    # -- rollback ----------------------------------------------------------

    def rollback(self, deployment_id: str, request: RollbackRequest) -> Deployment:
        """Roll back to a recorded revision whose migrations are a subset."""
        with self._sessions.begin() as session:
            row = self._get_row(session, deployment_id, request.org_id)
            current = DeploymentStatus(row.status)
            if current is DeploymentStatus.ROLLED_BACK:
                raise ValueError(f"deployment already rolled back: {deployment_id}")

            parent_id = request.target_deployment_id
            target = self._get_row(session, parent_id, request.org_id)

            # Rolling back to the deployment's OWN revision is meaningless.
            if target.deployment_id == row.deployment_id:
                raise ValueError(f"rollback target is the current deployment: {parent_id}")

            conflicts = rollback_conflicts(
                _load_list(row.migrations_json), _load_list(target.migrations_json)
            )
            if conflicts:
                raise ValueError(
                    f"rollback target {parent_id} predates migration(s) the current "
                    f"release applied: {', '.join(conflicts)}"
                )

            row.status = DeploymentStatus.ROLLED_BACK.value
            session.flush()
            audit.record(
                "deployments.rolled_back",
                "deployment-store",
                f"deployment:{deployment_id}",
                {
                    "org_id": request.org_id,
                    "from": current.value,
                    "target_deployment_id": parent_id,
                    "actor_ref": request.actor_ref,
                    "reason_ref": request.reason_ref,
                },
            )
            return _deployment_from_row(row)

    # -- release evidence --------------------------------------------------

    def record_evidence(
        self, deployment_id: str, request: RecordEvidenceRequest
    ) -> ReleaseEvidence:
        """Append-only release evidence, idempotent per (org, deployment_id)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            deployment = self._get_row(session, deployment_id, request.org_id)
            existing = session.scalars(
                sa.select(ReleaseEvidenceRecord).where(
                    ReleaseEvidenceRecord.org_id == request.org_id,
                    ReleaseEvidenceRecord.deployment_id == deployment_id,
                )
            ).first()
            if existing is not None:
                stored = _evidence_from_row(existing)
                if (
                    stored.commit_sha != request.commit_sha
                    or stored.image_digest != request.image_digest
                    or stored.config_version != request.config_version
                ):
                    raise ValueError(
                        f"release evidence already recorded with different fields: {deployment_id}"
                    )
                return stored

            rolled_back_at = (
                now if DeploymentStatus(deployment.status) is DeploymentStatus.ROLLED_BACK else None
            )
            row = ReleaseEvidenceRecord(
                deployment_id=deployment_id,
                org_id=request.org_id,
                commit_sha=request.commit_sha,
                image_digest=request.image_digest,
                migrations_json=_dump_list(list(request.migrations)),
                config_version=request.config_version,
                agent_versions_json=_dump_list(list(request.agent_versions)),
                policy_versions_json=_dump_list(list(request.policy_versions)),
                ci_runs_json=_dump_list(list(request.ci_runs)),
                security_scan_summary=request.security_scan_summary,
                deployed_at=now,
                rolled_back_at=rolled_back_at,
                rollback_target=request.rollback_target,
                operator_approvals_json=_dump_list(list(request.operator_approvals)),
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "release_evidence.recorded",
                "deployment-store",
                f"deployment:{deployment_id}",
                {
                    "org_id": request.org_id,
                    "commit_sha": request.commit_sha,
                    "ci_run_count": len(request.ci_runs),
                    "approval_count": len(request.operator_approvals),
                },
            )
            return _evidence_from_row(row)

    def get_evidence(self, deployment_id: str, org_id: str) -> ReleaseEvidence | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ReleaseEvidenceRecord).where(
                    ReleaseEvidenceRecord.org_id == org_id,
                    ReleaseEvidenceRecord.deployment_id == deployment_id,
                )
            ).first()
            return _evidence_from_row(row) if row is not None else None


router = APIRouter(tags=["deployments"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> DeploymentStore:
    return DeploymentStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if (
        "already" in message
        or "illegal" in message
        or "predates" in message
        or "has not passed" in message
        or "meaningless" in message
    ):
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post("/deployments", response_model=Deployment, name="register_deployment")
def register_deployment_route(
    request: RegisterDeploymentRequest,
    store: Annotated[DeploymentStore, Depends(_store)],
) -> Deployment:
    """Register one immutable deployment (idempotent)."""
    try:
        return store.register_deployment(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/deployments", response_model=list[Deployment], name="list_deployments")
def list_deployments_route(
    store: Annotated[DeploymentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    environment: Annotated[Environment | None, Query()] = None,
) -> list[Deployment]:
    """List deployments for one org, optionally filtered by environment."""
    return store.list_deployments(org_id, environment)


@router.get(
    "/deployments/{deployment_id}",
    response_model=Deployment,
    name="get_deployment",
)
def get_deployment_route(
    deployment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[DeploymentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Deployment:
    """Get one deployment by id and org."""
    stored = store.get_deployment(deployment_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"deployment not found: {deployment_id}")
    return stored


@router.post(
    "/deployments/{deployment_id}/canary",
    response_model=Deployment,
    name="start_deployment_canary",
)
def start_canary_route(
    deployment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: CanaryRequest,
    store: Annotated[DeploymentStore, Depends(_store)],
) -> Deployment:
    """Start the canary stage (idempotent)."""
    try:
        return store.start_canary(deployment_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/deployments/{deployment_id}/promote",
    response_model=Deployment,
    name="promote_deployment",
)
def promote_deployment_route(
    deployment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: PromoteRequest,
    store: Annotated[DeploymentStore, Depends(_store)],
) -> Deployment:
    """Promote a canary to full rollout after a passing health window."""
    try:
        return store.promote(deployment_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/deployments/{deployment_id}/fail",
    response_model=Deployment,
    name="fail_deployment",
)
def fail_deployment_route(
    deployment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: CanaryRequest,
    store: Annotated[DeploymentStore, Depends(_store)],
) -> Deployment:
    """Mark a rollout failed (enables rollback)."""
    try:
        return store.fail(deployment_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/deployments/{deployment_id}/rollback",
    response_model=Deployment,
    name="rollback_deployment",
)
def rollback_deployment_route(
    deployment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: RollbackRequest,
    store: Annotated[DeploymentStore, Depends(_store)],
) -> Deployment:
    """One-command rollback to a previously recorded revision."""
    try:
        return store.rollback(deployment_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/deployments/{deployment_id}/evidence",
    response_model=ReleaseEvidence,
    name="record_release_evidence",
)
def record_evidence_route(
    deployment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: RecordEvidenceRequest,
    store: Annotated[DeploymentStore, Depends(_store)],
) -> ReleaseEvidence:
    """Record the release evidence for one deployment (idempotent)."""
    try:
        return store.record_evidence(deployment_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/deployments/{deployment_id}/evidence",
    response_model=ReleaseEvidence,
    name="get_release_evidence",
)
def get_evidence_route(
    deployment_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[DeploymentStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> ReleaseEvidence:
    """Get the release evidence for one deployment."""
    stored = store.get_evidence(deployment_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"release evidence not found: {deployment_id}")
    return stored
