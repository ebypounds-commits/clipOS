"""Agent policy and budget configuration for immutable agent versions.

Versioned definitions carry default retry, escalation, memory, and budget
policies. Operators may attach one effective-dated override per version;
resolution always produces a complete typed policy while preserving the
default-versus-override precedence. Budget changes keep an effective-dated
history so a configuration can change over time without mutating the
immutable agent version. Material changes emit audit events.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.agent_registry import (
    AgentClass,
    AgentState,
    AgentVersionRecord,
    database_engine,
)
from clipos_api.repository import Base


class MemoryScope(str, Enum):
    """Memory scopes permitted by 04_AGENT_REGISTRY.md."""

    RUN = "run"
    WORKFLOW = "workflow"
    BRAND = "brand"
    AGENT = "agent"
    ORGANIZATION = "organization"
    SYSTEM = "system"


class RetryPolicy(BaseModel):
    """Typed retry contract replacing unvalidated dictionaries."""

    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(ge=1)
    backoff_seconds: float = Field(ge=0)
    exponential_base: float = Field(default=2.0, ge=1.0)


class EscalationPolicy(BaseModel):
    """Escalation target must be a registered agent class or a human lane."""

    model_config = ConfigDict(frozen=True)

    target: str = Field(min_length=1, max_length=64)
    notify: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _target_is_known(self) -> EscalationPolicy:
        allowed = {item.value for item in AgentClass} | {"human"}
        if self.target not in allowed:
            raise ValueError("escalation target must be a known agent_class or 'human'")
        return self


class MemoryPolicy(BaseModel):
    """Memory scopes and retention; default scopes stay inside allowed scopes."""

    model_config = ConfigDict(frozen=True)

    allowed_scopes: list[MemoryScope] = Field(min_length=1)
    default_scopes: list[MemoryScope] = Field(default_factory=list)
    retention_days: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_scopes(cls, data: Any) -> Any:
        """Map the version schema's ``scopes`` list onto allowed/default scopes."""
        if isinstance(data, dict) and "allowed_scopes" not in data and "scopes" in data:
            data = {**data, "allowed_scopes": data["scopes"], "default_scopes": data["scopes"]}
        return data

    @model_validator(mode="after")
    def _defaults_are_allowed(self) -> MemoryPolicy:
        if not set(self.default_scopes) <= set(self.allowed_scopes):
            raise ValueError("default memory scopes must be a subset of allowed scopes")
        return self


class BudgetConfiguration(BaseModel):
    """Effective-dated spend ceilings; absent fields mean 'not configured'."""

    model_config = ConfigDict(frozen=True)

    per_run: float | None = Field(default=None, ge=0)
    hourly: float | None = Field(default=None, ge=0)
    daily: float | None = Field(default=None, ge=0)
    monthly: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _configured_ceilings_are_ordered(self) -> BudgetConfiguration:
        values = (
            self.per_run,
            self.hourly,
            self.daily,
            self.monthly,
        )
        if any(
            current is not None and following is not None and current > following
            for current, following in itertools.pairwise(values)
        ):
            raise ValueError(
                "configured spend ceilings must be ordered per_run <= hourly <= daily <= monthly"
            )
        return self


class PolicyOverride(BaseModel):
    """An operator override; only supplied fields replace version defaults."""

    model_config = ConfigDict(frozen=True)

    retry_policy: RetryPolicy | None = None
    escalation_policy: EscalationPolicy | None = None
    memory_policy: MemoryPolicy | None = None
    budget: BudgetConfiguration | None = None
    effective_from: datetime


class EffectivePolicy(BaseModel):
    """The complete resolved policy for one agent version."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    version: str
    retry_policy: RetryPolicy
    escalation_policy: EscalationPolicy
    memory_policy: MemoryPolicy
    budget: BudgetConfiguration
    override_applied: bool


def _aware(value: datetime) -> datetime:
    """Normalize SQLite-loaded datetimes to UTC for comparisons."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class PolicyOverrideRecord(Base):
    """SQL row for the current policy override of an agent version."""

    __tablename__ = "agent_policy_overrides"

    agent_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    version: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    policies: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    effective_from: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class AgentBudgetConfigurationRecord(Base):
    """SQL row for one effective-dated budget configuration."""

    __tablename__ = "agent_budget_configs"
    __table_args__ = (
        sa.Index(
            "ix_agent_budget_configs_lookup",
            "agent_id",
            "version",
            "effective_from",
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    version: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    budget: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    effective_from: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    effective_to: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _material_payload(override: PolicyOverride) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in ("retry_policy", "escalation_policy", "memory_policy", "budget"):
        value = getattr(override, field)
        payload[field] = value.model_dump() if value is not None else None
    return payload


def _default_budget(record: AgentVersionRecord) -> BudgetConfiguration:
    return BudgetConfiguration.model_validate(record.spend_ceilings)


def _default_policies(
    record: AgentVersionRecord,
) -> tuple[RetryPolicy, EscalationPolicy, MemoryPolicy, BudgetConfiguration]:
    return (
        RetryPolicy.model_validate(record.retry_policy),
        EscalationPolicy.model_validate(record.escalation_policy),
        MemoryPolicy.model_validate(record.memory_policy),
        _default_budget(record),
    )


class AgentPolicyStore:
    """Session-bound SQL persistence for policy overrides and budget history."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> AgentPolicyStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def _require_version(self, session: Session, agent_id: str, version: str) -> AgentVersionRecord:
        record = session.get(AgentVersionRecord, {"agent_id": agent_id, "version": version})
        if record is None:
            raise ValueError(f"agent version not found: {agent_id}:{version}")
        return record

    @staticmethod
    def _require_mutable(record: AgentVersionRecord) -> AgentVersionRecord:
        """Reject lifecycle-frozen versions from material configuration changes."""
        state = AgentState(record.state)
        if state in {AgentState.SUSPENDED, AgentState.RETIRED}:
            raise ValueError(f"agent version is {state.value}; configuration is lifecycle-frozen")
        return record

    def set_override(
        self, agent_id: str, version: str, override: PolicyOverride
    ) -> EffectivePolicy:
        """Attach or update the version override, auditing a material change."""
        payload = _material_payload(override)
        with self._sessions.begin() as session:
            self._require_mutable(self._require_version(session, agent_id, version))
            now = datetime.now(UTC)
            row = session.get(PolicyOverrideRecord, {"agent_id": agent_id, "version": version})
            previous_payload = row.policies if row is not None else None
            if row is None:
                row = PolicyOverrideRecord(
                    agent_id=agent_id,
                    version=version,
                    policies=payload,
                    effective_from=override.effective_from,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            elif row.policies == payload and _aware(row.effective_from) == override.effective_from:
                return self.resolve(agent_id, version)
            else:
                row.policies = payload
                row.effective_from = override.effective_from
                row.updated_at = now
            session.flush()
        audit.record(
            "agent_policy.override_changed",
            "registry-operator",
            f"agent:{agent_id}:{version}",
            {
                "from": previous_payload,
                "to": payload,
                "effective_from": override.effective_from.isoformat(),
            },
        )
        return self.resolve(agent_id, version)

    def update_budget(
        self,
        agent_id: str,
        version: str,
        budget: BudgetConfiguration,
        effective_from: datetime,
    ) -> BudgetConfiguration:
        """Append an effective-dated budget, superseding the previous window."""
        payload = budget.model_dump()
        with self._sessions.begin() as session:
            self._require_mutable(self._require_version(session, agent_id, version))
            now = datetime.now(UTC)
            open_rows = session.scalars(
                sa.select(AgentBudgetConfigurationRecord)
                .where(
                    AgentBudgetConfigurationRecord.agent_id == agent_id,
                    AgentBudgetConfigurationRecord.version == version,
                    AgentBudgetConfigurationRecord.effective_to.is_(None),
                )
                .order_by(AgentBudgetConfigurationRecord.effective_from)
            ).all()
            if (
                open_rows
                and open_rows[-1].budget == payload
                and (_aware(open_rows[-1].effective_from) == effective_from)
            ):
                return BudgetConfiguration.model_validate(payload)
            previous_payload = open_rows[-1].budget if open_rows else None
            if open_rows:
                if effective_from <= _aware(open_rows[-1].effective_from):
                    raise ValueError(
                        "budget effective_from must be after the current configuration start"
                    )
                for row in open_rows:
                    row.effective_to = effective_from
                    row.updated_at = now
            session.add(
                AgentBudgetConfigurationRecord(
                    agent_id=agent_id,
                    version=version,
                    budget=payload,
                    effective_from=effective_from,
                    effective_to=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
        audit.record(
            "agent_policy.budget_changed",
            "registry-operator",
            f"agent:{agent_id}:{version}",
            {
                "from": previous_payload,
                "to": payload,
                "effective_from": effective_from.isoformat(),
                "superseded_count": len(open_rows),
            },
        )
        return budget

    def resolve(self, agent_id: str, version: str, at: datetime | None = None) -> EffectivePolicy:
        """Resolve explicit override fields over immutable version defaults."""
        moment = at or datetime.now(UTC)
        with self._sessions() as session:
            version_record = self._require_version(session, agent_id, version)
            retry, escalation, memory, budget = _default_policies(version_record)
            budget_row = session.scalars(
                sa.select(AgentBudgetConfigurationRecord)
                .where(
                    AgentBudgetConfigurationRecord.agent_id == agent_id,
                    AgentBudgetConfigurationRecord.version == version,
                    AgentBudgetConfigurationRecord.effective_from <= moment,
                    (AgentBudgetConfigurationRecord.effective_to.is_(None))
                    | (AgentBudgetConfigurationRecord.effective_to > moment),
                )
                .order_by(AgentBudgetConfigurationRecord.effective_from.desc())
            ).first()
            if budget_row is not None:
                budget = BudgetConfiguration.model_validate(budget_row.budget)
            override_row = session.get(
                PolicyOverrideRecord, {"agent_id": agent_id, "version": version}
            )
            applied = False
            if override_row is not None and _aware(override_row.effective_from) <= moment:
                applied = True
                policies = override_row.policies
                if policies["retry_policy"] is not None:
                    retry = RetryPolicy.model_validate(policies["retry_policy"])
                if policies["escalation_policy"] is not None:
                    escalation = EscalationPolicy.model_validate(policies["escalation_policy"])
                if policies["memory_policy"] is not None:
                    memory = MemoryPolicy.model_validate(policies["memory_policy"])
                if policies["budget"] is not None and budget_row is None:
                    budget = BudgetConfiguration.model_validate(policies["budget"])
        return EffectivePolicy(
            agent_id=agent_id,
            version=version,
            retry_policy=retry,
            escalation_policy=escalation,
            memory_policy=memory,
            budget=budget,
            override_applied=applied,
        )

    def get_override(self, agent_id: str, version: str) -> PolicyOverride | None:
        """Return the current override, or None."""
        with self._sessions() as session:
            row = session.get(PolicyOverrideRecord, {"agent_id": agent_id, "version": version})
            if row is None:
                return None
            return _override_from_payload(row.policies, _aware(row.effective_from))


def _override_from_payload(payload: dict[str, Any], effective_from: datetime) -> PolicyOverride:
    return PolicyOverride(
        retry_policy=(
            RetryPolicy.model_validate(payload["retry_policy"])
            if payload["retry_policy"] is not None
            else None
        ),
        escalation_policy=(
            EscalationPolicy.model_validate(payload["escalation_policy"])
            if payload["escalation_policy"] is not None
            else None
        ),
        memory_policy=(
            MemoryPolicy.model_validate(payload["memory_policy"])
            if payload["memory_policy"] is not None
            else None
        ),
        budget=(
            BudgetConfiguration.model_validate(payload["budget"])
            if payload["budget"] is not None
            else None
        ),
        effective_from=effective_from,
    )


def _policy_store(engine: Annotated[Engine, Depends(database_engine)]) -> AgentPolicyStore:
    return AgentPolicyStore.from_engine(engine)


def set_override_route(
    agent_id: str,
    version: str,
    override: PolicyOverride,
    store: Annotated[AgentPolicyStore, Depends(_policy_store)],
) -> EffectivePolicy:
    """Attach or update a policy override for one version."""
    try:
        return store.set_override(agent_id, version, override)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def resolve_effective_policy_route(
    agent_id: str,
    version: str,
    store: Annotated[AgentPolicyStore, Depends(_policy_store)],
) -> EffectivePolicy:
    """Return the effective resolved policy for one version."""
    try:
        return store.resolve(agent_id, version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class BudgetUpdateRequest(BaseModel):
    """POST payload for an effective-dated budget update."""

    model_config = ConfigDict(frozen=True)

    budget: BudgetConfiguration
    effective_from: datetime


def update_budget_route(
    agent_id: str,
    version: str,
    request: BudgetUpdateRequest,
    store: Annotated[AgentPolicyStore, Depends(_policy_store)],
) -> BudgetConfiguration:
    """Append an effective-dated budget configuration."""
    try:
        return store.update_budget(agent_id, version, request.budget, request.effective_from)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


router = APIRouter(prefix="/agent-policies", tags=["agent-policies"])
router.add_api_route(
    "/{agent_id}/{version}/policy-override",
    set_override_route,
    methods=["POST"],
    name="set_agent_policy_override",
)
router.add_api_route(
    "/{agent_id}/{version}/effective-policy",
    resolve_effective_policy_route,
    methods=["GET"],
    name="resolve_agent_effective_policy",
)
router.add_api_route(
    "/{agent_id}/{version}/budget",
    update_budget_route,
    methods=["POST"],
    name="update_agent_budget",
)
