"""Agent Registry: typed definitions, durable lifecycle, and logical-model routing.

Agent versions are immutable and always reference logical model aliases; concrete
provider identifiers live only in the separately configured model-alias-target
table. Lifecycle changes use constrained transitions, material changes emit audit
events, and HIGH-risk activation is denied unless a human approval flag is
explicitly supplied.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base, get_engine

_LOGICAL_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")


class AgentClass(str, Enum):
    """The exact agent classes from 04_AGENT_REGISTRY.md."""

    RESEARCH = "research"
    PLANNING = "planning"
    CREATIVE = "creative"
    MEDIA_ANALYSIS = "media_analysis"
    COMPLIANCE = "compliance"
    PUBLISHING = "publishing"
    ANALYTICS = "analytics"
    FINANCE = "finance"
    OPERATIONS = "operations"
    GOVERNANCE = "governance"
    ENGINEERING_PROPOSAL = "engineering_proposal"
    EVALUATION = "evaluation"


class AgentState(str, Enum):
    """Ordered activation lifecycle values from 04_AGENT_REGISTRY.md."""

    DRAFT = "DRAFT"
    EVALUATING = "EVALUATING"
    CANARY = "CANARY"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


class RiskClassification(str, Enum):
    """Deterministic agent risk tier used by the activation gate."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RequiredCapabilities(BaseModel):
    """Capabilities the model route and runtime must support."""

    model_config = ConfigDict(frozen=True)

    reasoning: bool = False
    vision: bool = False
    structured_output: bool = False
    min_context_tokens: int = Field(default=0, ge=0)
    tool_use: bool = False


class SpendCeilings(BaseModel):
    """Monetary ceilings enforced by the budget service, not by model judgment."""

    model_config = ConfigDict(frozen=True)

    per_run: float = Field(ge=0)
    hourly: float = Field(ge=0)
    daily: float = Field(ge=0)
    monthly: float = Field(ge=0)


class EvaluatorRequirement(BaseModel):
    """A named evaluator and the minimum score required to pass."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=255)
    required_score: float = Field(ge=0, le=1)


class AgentVersion(BaseModel):
    """Immutable agent definition contract from 04_AGENT_REGISTRY.md.

    ``version`` is None in a bare definition; the registry stamps the
    registered version identity onto every returned model.
    """

    model_config = ConfigDict(frozen=True)

    version: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1)
    agent_class: AgentClass
    prompt_version_ref: str = Field(min_length=1, max_length=255)
    model_alias: str = Field(
        min_length=1,
        max_length=255,
        pattern=_LOGICAL_ALIAS_PATTERN.pattern,
    )

    fallback_aliases: list[str] = Field(default_factory=list)
    required_capabilities: RequiredCapabilities
    tool_grants: list[str] = Field(default_factory=list)
    prohibited_tools: list[str] = Field(default_factory=list)
    memory_policy: dict[str, Any]
    max_iterations: int = Field(ge=0)
    max_tool_calls: int = Field(ge=0)
    timeout_seconds: float = Field(gt=0)
    spend_ceilings: SpendCeilings
    concurrency_ceiling: int = Field(ge=0)
    evaluators: list[EvaluatorRequirement] = Field(min_length=1)
    retry_policy: dict[str, Any]
    escalation_policy: dict[str, Any]
    risk_classification: RiskClassification
    activation_state: AgentState = AgentState.DRAFT
    canary_percentage: int = Field(default=0, ge=0, le=100)

    @field_validator("fallback_aliases")
    @classmethod
    def _fallback_aliases_are_logical(cls, value: list[str]) -> list[str]:
        """Require the same logical-alias namespace as the primary model alias."""
        if any(not _LOGICAL_ALIAS_PATTERN.fullmatch(item) for item in value):
            raise ValueError("fallback_aliases must contain logical aliases only")
        return value

    @field_validator("spend_ceilings")
    @classmethod
    def _ceilings_are_ordered(cls, value: SpendCeilings) -> SpendCeilings:
        if (
            value.per_run > value.hourly
            or value.hourly > value.daily
            or value.daily > value.monthly
        ):
            raise ValueError("spend ceilings must be ordered per_run <= hourly <= daily <= monthly")
        return value


class ActivationRequest(BaseModel):
    """Activation payload; HIGH risk requires an explicit human approval flag."""

    model_config = ConfigDict(frozen=True)

    version: str = Field(min_length=1, max_length=128)
    approved_high_risk: bool = False


class AgentRecord(BaseModel):
    """An agent's stable identity, active pointer, and immutable versions."""

    model_config = ConfigDict(frozen=True)

    agent_id: str = Field(min_length=1, max_length=128)
    active_version: str | None = Field(default=None, max_length=128)
    versions: list[AgentVersion]


class ModelAliasTarget(BaseModel):
    """One ordered, database-configured model destination for a logical alias."""

    model_config = ConfigDict(frozen=True)

    alias: str = Field(min_length=1, max_length=255)
    target_id: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=255)
    model_id: str = Field(min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=255)
    endpoint: str | None = Field(default=None, max_length=255)
    priority: int = Field(ge=0)
    enabled: bool = True


_LOGICAL_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")

_LEGAL_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.DRAFT: {AgentState.EVALUATING},
    AgentState.EVALUATING: {AgentState.CANARY},
    AgentState.CANARY: {AgentState.ACTIVE},
    AgentState.ACTIVE: {AgentState.SUSPENDED},
    AgentState.SUSPENDED: {AgentState.RETIRED},
}


def _validate_transition(current: AgentState, target: AgentState) -> None:
    if target not in _LEGAL_TRANSITIONS.get(current, set()):
        raise ValueError(f"illegal lifecycle transition: {current.value} -> {target.value}")


class AgentVersionRecord(Base):
    """SQL row for an immutable agent version and its lifecycle state."""

    __tablename__ = "agent_versions"
    __table_args__ = (
        sa.Index("uq_agent_versions_agent_version", "agent_id", "version", unique=True),
        sa.Index("ix_agent_versions_agent_state", "agent_id", "state"),
    )

    agent_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    version: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    agent_ref: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    agent_class: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    prompt_version_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    model_alias: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    fallback_aliases: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False)
    required_capabilities: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    tool_grants: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False)
    prohibited_tools: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False)
    memory_policy: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    max_iterations: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    max_tool_calls: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(sa.Float, nullable=False)
    spend_ceilings: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    concurrency_ceiling: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    evaluators: Mapped[list[dict[str, Any]]] = mapped_column(sa.JSON, nullable=False)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    escalation_policy: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    risk_classification: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    canary_percentage: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class AgentRecordModel(Base):
    """SQL row for the agent's active-version pointer."""

    __tablename__ = "agent_records"

    agent_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    active_version: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ModelAliasTargetRecord(Base):
    """SQL row for ordered model alias targets."""

    __tablename__ = "model_alias_targets"
    __table_args__ = (
        sa.Index("uq_model_alias_targets_alias_id", "alias", "target_id", unique=True),
        sa.Index("ix_model_alias_targets_alias_priority", "alias", "priority"),
    )

    target_id: Mapped[str] = mapped_column(sa.String(255), primary_key=True)
    alias: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    model_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    region: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)


def _record_version(
    contract: AgentVersion, agent_id: str, version: str, now: datetime
) -> AgentVersionRecord:
    return AgentVersionRecord(
        agent_id=agent_id,
        version=version,
        agent_ref=agent_id,
        name=contract.name,
        purpose=contract.purpose,
        agent_class=contract.agent_class.value,
        prompt_version_ref=contract.prompt_version_ref,
        model_alias=contract.model_alias,
        fallback_aliases=contract.fallback_aliases,
        required_capabilities=contract.required_capabilities.model_dump(),
        tool_grants=contract.tool_grants,
        prohibited_tools=contract.prohibited_tools,
        memory_policy=contract.memory_policy,
        max_iterations=contract.max_iterations,
        max_tool_calls=contract.max_tool_calls,
        timeout_seconds=contract.timeout_seconds,
        spend_ceilings=contract.spend_ceilings.model_dump(),
        concurrency_ceiling=contract.concurrency_ceiling,
        evaluators=[item.model_dump() for item in contract.evaluators],
        retry_policy=contract.retry_policy,
        escalation_policy=contract.escalation_policy,
        risk_classification=contract.risk_classification.value,
        state=contract.activation_state.value,
        canary_percentage=contract.canary_percentage,
        created_at=now,
        updated_at=now,
    )


def _model_version(record: AgentVersionRecord) -> AgentVersion:
    return AgentVersion(
        version=record.version,
        name=record.name,
        purpose=record.purpose,
        agent_class=AgentClass(record.agent_class),
        prompt_version_ref=record.prompt_version_ref,
        model_alias=record.model_alias,
        fallback_aliases=record.fallback_aliases,
        required_capabilities=RequiredCapabilities.model_validate(record.required_capabilities),
        tool_grants=record.tool_grants,
        prohibited_tools=record.prohibited_tools,
        memory_policy=record.memory_policy,
        max_iterations=record.max_iterations,
        max_tool_calls=record.max_tool_calls,
        timeout_seconds=record.timeout_seconds,
        spend_ceilings=SpendCeilings.model_validate(record.spend_ceilings),
        concurrency_ceiling=record.concurrency_ceiling,
        evaluators=[EvaluatorRequirement.model_validate(item) for item in record.evaluators],
        retry_policy=record.retry_policy,
        escalation_policy=record.escalation_policy,
        risk_classification=RiskClassification(record.risk_classification),
        activation_state=AgentState(record.state),
        canary_percentage=record.canary_percentage,
    )


def _definition_signature(contract: AgentVersion) -> dict[str, Any]:
    """Exclude mutable lifecycle state from immutable-version comparisons."""
    data = contract.model_dump()
    data.pop("activation_state")
    data.pop("version", None)  # identity supplied by the registry, not the definition
    return data


def _audit(action: str, actor: str, subject: str, details: dict[str, Any]) -> dict[str, Any]:
    return audit.record(action=action, actor=actor, subject=subject, details=details)


def _audit_state_change(
    record: AgentVersionRecord, agent_id: str, version: str, target: AgentState
) -> None:
    _audit(
        "agent_registry.state_changed",
        "registry-operator",
        f"agent:{agent_id}:{version}",
        {
            "from": AgentState(record.state).value,
            "to": target.value,
            "risk": record.risk_classification,
            "canary_percentage": record.canary_percentage,
            "approved_high_risk": target == AgentState.ACTIVE
            and record.risk_classification == RiskClassification.HIGH.value,
        },
    )


class AgentRegistryStore:
    """Session-bound SQL persistence for agent and model-alias routing data."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> AgentRegistryStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_version_in_transaction(
        self, session: Session, agent_id: str, version: str, definition: AgentVersion
    ) -> AgentVersion:
        """Idempotently register a version in the caller's transaction."""
        if definition.activation_state != AgentState.DRAFT:
            raise ValueError("new agent versions must start in DRAFT")
        now = datetime.now(UTC)
        record = session.get(AgentVersionRecord, {"agent_id": agent_id, "version": version})
        if record is not None:
            stored = _model_version(record)
            if _definition_signature(stored) != _definition_signature(definition):
                raise ValueError("agent version already registered with a different definition")
            return stored
        row = _record_version(definition, agent_id, version, now)
        session.add(row)
        agent_record = session.get(AgentRecordModel, agent_id)
        if agent_record is None:
            session.add(
                AgentRecordModel(
                    agent_id=agent_id, active_version=None, created_at=now, updated_at=now
                )
            )
        else:
            agent_record.updated_at = now
        session.flush()
        _audit(
            "agent_registry.version_registered",
            "registry-operator",
            f"agent:{agent_id}:{version}",
            {
                "agent_class": definition.agent_class.value,
                "model_alias": definition.model_alias,
                "fallback_aliases": definition.fallback_aliases,
                "risk_classification": definition.risk_classification.value,
            },
        )
        return _model_version(row)

    def register_version(
        self, agent_id: str, version: str, definition: AgentVersion
    ) -> AgentVersion:
        """Register an immutable version; a matching re-registration is a no-op."""
        with self._sessions.begin() as session:
            return self.register_version_in_transaction(session, agent_id, version, definition)

    def transition_in_transaction(
        self,
        session: Session,
        agent_id: str,
        version: str,
        target: AgentState,
        approved_high_risk: bool = False,
    ) -> AgentVersion:
        """Apply one legal lifecycle transition inside the caller's transaction."""
        record = session.get(AgentVersionRecord, {"agent_id": agent_id, "version": version})
        if record is None:
            raise ValueError(f"agent version not found: {agent_id}:{version}")
        current = AgentState(record.state)
        if current == target:
            return _model_version(record)
        _validate_transition(current, target)
        if (
            target == AgentState.ACTIVE
            and record.risk_classification == RiskClassification.HIGH.value
            and not approved_high_risk
        ):
            raise ValueError("HIGH risk agent activation requires explicit human approval")
        if target == AgentState.ACTIVE:
            active_owner = session.scalars(
                sa.select(AgentVersionRecord.version).where(
                    AgentVersionRecord.agent_id == agent_id,
                    AgentVersionRecord.state == AgentState.ACTIVE.value,
                )
            ).first()
            if active_owner is not None and active_owner != version:
                raise ValueError("another version is already ACTIVE; suspend it first")
        agent_record = session.get(AgentRecordModel, agent_id)
        if agent_record is None:
            raise ValueError(f"agent not found: {agent_id}")
        previous_pointer = agent_record.active_version
        if target == AgentState.ACTIVE:
            agent_record.active_version = version
        elif target in {AgentState.SUSPENDED, AgentState.RETIRED} and (
            agent_record.active_version == version
        ):
            agent_record.active_version = None
        agent_record.updated_at = datetime.now(UTC)
        record.state = target.value
        record.updated_at = agent_record.updated_at
        if target == AgentState.CANARY and record.canary_percentage == 0:
            record.canary_percentage = 1
        session.flush()
        _audit_state_change(record, agent_id, version, target)
        if previous_pointer != agent_record.active_version:
            _audit(
                "agent_registry.active_pointer_changed",
                "registry-operator",
                f"agent:{agent_id}",
                {
                    "from_version": previous_pointer,
                    "to_version": agent_record.active_version,
                    "state": target.value,
                },
            )
        return _model_version(record)

    def transition(
        self,
        agent_id: str,
        version: str,
        target: AgentState,
        approved_high_risk: bool = False,
    ) -> AgentVersion:
        """Apply one legal lifecycle transition and commit it."""
        with self._sessions.begin() as session:
            return self.transition_in_transaction(
                session, agent_id, version, target, approved_high_risk
            )

    def activate(
        self, agent_id: str, version: str, approved_high_risk: bool = False
    ) -> AgentVersion:
        """Activate a version, requiring prior EVALUATING and CANARY gates."""
        return self.transition(agent_id, version, AgentState.ACTIVE, approved_high_risk)

    def set_canary_percentage(self, agent_id: str, version: str, percentage: int) -> AgentVersion:
        """Set a CANARY version's traffic percentage and audit a material change."""
        if not 0 <= percentage <= 100:
            raise ValueError("canary percentage must be between 0 and 100")
        with self._sessions.begin() as session:
            record = session.get(AgentVersionRecord, {"agent_id": agent_id, "version": version})
            if record is None:
                raise ValueError(f"agent version not found: {agent_id}:{version}")
            if AgentState(record.state) != AgentState.CANARY:
                raise ValueError("canary percentage can only be set while in CANARY")
            previous = record.canary_percentage
            if previous == percentage:
                return _model_version(record)
            record.canary_percentage = percentage
            record.updated_at = datetime.now(UTC)
            session.flush()
        _audit(
            "agent_registry.canary_percentage_changed",
            "registry-operator",
            f"agent:{agent_id}:{version}",
            {"from": previous, "to": percentage},
        )
        return _model_version(record)

    def resolve(self, agent_id: str) -> AgentVersion | None:
        """Return the currently active agent version, or None."""
        with self._sessions() as session:
            agent_record = session.get(AgentRecordModel, agent_id)
            if agent_record is None or agent_record.active_version is None:
                return None
            version_record = session.get(
                AgentVersionRecord,
                {"agent_id": agent_id, "version": agent_record.active_version},
            )
            if version_record is None:
                return None
            return _model_version(version_record)

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        """Return the agent record and all immutable versions."""
        with self._sessions() as session:
            agent_record = session.get(AgentRecordModel, agent_id)
            if agent_record is None:
                return None
            rows = session.scalars(
                sa.select(AgentVersionRecord)
                .where(AgentVersionRecord.agent_id == agent_id)
                .order_by(AgentVersionRecord.created_at, AgentVersionRecord.version)
            ).all()
            return AgentRecord(
                agent_id=agent_id,
                active_version=agent_record.active_version,
                versions=[_model_version(row) for row in rows],
            )

    def set_alias_targets(self, targets: list[ModelAliasTarget]) -> None:
        """Atomically replace all targets for each supplied logical alias."""
        if not targets:
            return
        aliases = sorted({item.alias for item in targets})
        with self._sessions.begin() as session:
            session.execute(
                sa.delete(ModelAliasTargetRecord).where(ModelAliasTargetRecord.alias.in_(aliases))
            )
            for target in targets:
                session.add(
                    ModelAliasTargetRecord(
                        target_id=target.target_id,
                        alias=target.alias,
                        provider=target.provider,
                        model_id=target.model_id,
                        region=target.region,
                        endpoint=target.endpoint,
                        priority=target.priority,
                        enabled=target.enabled,
                    )
                )
            session.flush()
            for alias in aliases:
                rows = session.scalars(
                    sa.select(ModelAliasTargetRecord)
                    .where(ModelAliasTargetRecord.alias == alias)
                    .order_by(ModelAliasTargetRecord.priority)
                ).all()
                _audit(
                    "agent_registry.alias_targets_changed",
                    "registry-operator",
                    f"model_alias:{alias}",
                    {
                        "enabled_targets": [
                            {"target_id": row.target_id, "priority": row.priority} for row in rows
                        ]
                    },
                )

    def resolve_model_alias(self, alias: str) -> list[ModelAliasTarget]:
        """Return enabled targets in configured priority order."""
        if not alias.strip():
            raise ValueError("alias must be non-empty")
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ModelAliasTargetRecord)
                .where(ModelAliasTargetRecord.alias == alias)
                .where(ModelAliasTargetRecord.enabled.is_(True))
                .order_by(ModelAliasTargetRecord.priority, ModelAliasTargetRecord.target_id)
            ).all()
            return [
                ModelAliasTarget(
                    alias=row.alias,
                    target_id=row.target_id,
                    provider=row.provider,
                    model_id=row.model_id,
                    region=row.region,
                    endpoint=row.endpoint,
                    priority=row.priority,
                    enabled=row.enabled,
                )
                for row in rows
            ]


def database_engine() -> Engine:
    """Return the process-cached engine configured by ``DATABASE_URL``."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise HTTPException(status_code=503, detail="database not configured")
    try:
        return get_engine(url)
    except sa.exc.ArgumentError as exc:
        raise HTTPException(status_code=503, detail="database configuration invalid") from exc


def _registry_store(engine: Annotated[Engine, Depends(database_engine)]) -> AgentRegistryStore:
    return AgentRegistryStore.from_engine(engine)


class RegisterVersionRequest(BaseModel):
    """POST payload for immutable version registration."""

    model_config = ConfigDict(frozen=True)

    agent_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    definition: AgentVersion


def register_version(
    request: RegisterVersionRequest,
    registry: Annotated[AgentRegistryStore, Depends(_registry_store)],
) -> AgentVersion:
    """Register an immutable agent version (idempotent on matching definition)."""
    try:
        return registry.register_version(request.agent_id, request.version, request.definition)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def get_agent(
    agent_id: str, registry: Annotated[AgentRegistryStore, Depends(_registry_store)]
) -> AgentRecord:
    """Return the agent's identity, active pointer, and versions."""
    record = registry.get_agent(agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return record


def resolve_agent(
    agent_id: str, registry: Annotated[AgentRegistryStore, Depends(_registry_store)]
) -> AgentVersion:
    """Return the currently active version for an agent."""
    resolved = registry.resolve(agent_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="active agent version not found")
    return resolved


def activate_agent(
    agent_id: str,
    request: ActivationRequest,
    registry: Annotated[AgentRegistryStore, Depends(_registry_store)],
) -> AgentVersion:
    """Activate a version, enforcing lifecycle order and the HIGH-risk gate."""
    try:
        return registry.activate(
            agent_id, request.version, approved_high_risk=request.approved_high_risk
        )
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


router = APIRouter(prefix="/agents", tags=["agents"])
router.add_api_route("", register_version, methods=["POST"], name="register_agent_version")
router.add_api_route("/{agent_id}", get_agent, methods=["GET"], name="get_agent")
router.add_api_route(
    "/{agent_id}/activate", activate_agent, methods=["POST"], name="activate_agent"
)
router.add_api_route("/{agent_id}/resolve", resolve_agent, methods=["GET"], name="resolve_agent")
