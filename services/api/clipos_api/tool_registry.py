"""Tool Registry: stable tools, agent-version capability grants, and audits.

Tools are registered by natural key ``tool_id`` and definitions are immutable.
Capability grants attach tools to immutable agent versions; ``check_capability``
combines tool enablement, the registered version, an active grant, and the
version's explicit prohibition list.  Every grant/revoke transition is audited.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.agent_registry import AgentRegistryStore, database_engine
from clipos_api.repository import Base

_TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_DEFAULT_ACTOR = "registry-operator"


class ToolDef(BaseModel):
    """Immutable definition contract for one executable tool."""

    model_config = ConfigDict(frozen=True)

    tool_id: str = Field(min_length=1, max_length=255, pattern=_TOOL_ID_PATTERN.pattern)
    name: str = Field(min_length=1, max_length=255)
    capability_scopes: list[str] = Field(default_factory=list)
    budget_per_run: float = Field(ge=0)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class CapabilityGrantRequest(BaseModel):
    """Grant/revoke payload identifying one immutable agent version."""

    model_config = ConfigDict(frozen=True)

    agent_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)


class CapabilityGrant(BaseModel):
    """One agent-version grant and its active state."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    version: str
    tool_id: str
    active: bool
    granted_at: datetime
    revoked_at: datetime | None = None


class ToolRecordModel(Base):
    """SQL row for one registered tool."""

    __tablename__ = "tools"
    __table_args__ = (sa.Index("uq_tools_tool_id", "tool_id", unique=True),)

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    tool_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    capability_scopes: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False)
    budget_per_run: Mapped[float] = mapped_column(sa.Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    tool_metadata: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class CapabilityGrantRecord(Base):
    """SQL row for one agent-version/tool grant."""

    __tablename__ = "tool_capability_grants"
    __table_args__ = (
        sa.Index(
            "uq_tool_capability_grants_identity",
            "agent_id",
            "version",
            "tool_id",
            unique=True,
        ),
        sa.Index("ix_tool_capability_grants_tool_active", "tool_id", "active"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    version: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    tool_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    granted_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=True)


def _aware(value: datetime) -> datetime:
    """Normalize values read from backends that strip the timezone (SQLite)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _tool_from_row(row: ToolRecordModel) -> ToolDef:
    return ToolDef(
        tool_id=row.tool_id,
        name=row.name,
        capability_scopes=row.capability_scopes,
        budget_per_run=row.budget_per_run,
        enabled=row.enabled,
        metadata=row.tool_metadata,
    )


def _grant_from_row(row: CapabilityGrantRecord) -> CapabilityGrant:
    return CapabilityGrant(
        agent_id=row.agent_id,
        version=row.version,
        tool_id=row.tool_id,
        active=row.active,
        granted_at=_aware(row.granted_at),
        revoked_at=_aware(row.revoked_at) if row.revoked_at is not None else None,
    )


def _definition_signature(tool: ToolDef) -> dict[str, Any]:
    """Exclude mutable enable state from immutable-definition comparisons."""
    data = tool.model_dump()
    data.pop("enabled")
    return data


def _audit(action: str, subject: str, details: dict[str, Any]) -> dict[str, Any]:
    return audit.record(action=action, actor=_DEFAULT_ACTOR, subject=subject, details=details)


class ToolRegistryStore:
    """Session-bound SQL persistence for tools and capability grants."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ToolRegistryStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    @staticmethod
    def _tool(session: Session, tool_id: str) -> ToolRecordModel | None:
        return session.scalars(
            sa.select(ToolRecordModel).where(ToolRecordModel.tool_id == tool_id)
        ).first()

    @staticmethod
    def _grant_row(
        session: Session, agent_id: str, version: str, tool_id: str
    ) -> CapabilityGrantRecord | None:
        return session.scalars(
            sa.select(CapabilityGrantRecord).where(
                CapabilityGrantRecord.agent_id == agent_id,
                CapabilityGrantRecord.version == version,
                CapabilityGrantRecord.tool_id == tool_id,
            )
        ).first()

    def register_in_transaction(self, session: Session, tool: ToolDef) -> ToolDef:
        """Idempotently register one tool; conflicting immutable fields fail."""
        now = datetime.now(UTC)
        row = self._tool(session, tool.tool_id)
        if row is not None:
            stored = _tool_from_row(row)
            if _definition_signature(stored) != _definition_signature(tool):
                raise ValueError(
                    f"tool already registered with a different definition: {tool.tool_id}"
                )
            return stored
        row = ToolRecordModel(
            tool_id=tool.tool_id,
            name=tool.name,
            capability_scopes=tool.capability_scopes,
            budget_per_run=tool.budget_per_run,
            enabled=tool.enabled,
            tool_metadata=tool.metadata,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        _audit(
            "tool_registry.registered",
            f"tool:{tool.tool_id}",
            {
                "capability_scopes": tool.capability_scopes,
                "budget_per_run": tool.budget_per_run,
                "enabled": tool.enabled,
            },
        )
        return _tool_from_row(row)

    def register(self, tool: ToolDef) -> ToolDef:
        """Register one tool; a matching re-registration is a no-op."""
        with self._sessions.begin() as session:
            return self.register_in_transaction(session, tool)

    def list_tools(self) -> list[ToolDef]:
        """Return every registered tool ordered by stable tool_id."""
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ToolRecordModel).order_by(ToolRecordModel.tool_id)
            ).all()
            return [_tool_from_row(row) for row in rows]

    def get_tool(self, tool_id: str) -> ToolDef | None:
        """Return one tool, or None when it is not registered."""
        with self._sessions() as session:
            row = self._tool(session, tool_id)
            return _tool_from_row(row) if row is not None else None

    def set_tool_enabled(self, tool_id: str, enabled: bool) -> ToolDef:
        """Enable/disable one tool and disable-time drop its active grants."""
        action = "tool_registry.enabled" if enabled else "tool_registry.disabled"
        with self._sessions.begin() as session:
            row = self._tool(session, tool_id)
            if row is None:
                raise ValueError(f"tool not found: {tool_id}")
            if row.enabled == enabled:
                return _tool_from_row(row)
            row.enabled = enabled
            row.updated_at = datetime.now(UTC)
            dropped: list[dict[str, str]] = []
            if not enabled:
                grants = session.scalars(
                    sa.select(CapabilityGrantRecord).where(
                        CapabilityGrantRecord.tool_id == tool_id,
                        CapabilityGrantRecord.active.is_(True),
                    )
                ).all()
                for grant in grants:
                    grant.active = False
                    grant.revoked_at = row.updated_at
                    dropped.append({"agent_id": grant.agent_id, "version": grant.version})
            session.flush()
        _audit(action, f"tool:{tool_id}", {"enabled": enabled, "dropped_grants": dropped})
        return _tool_from_row(row)

    def _require_agent_version(self, session: Session, agent_id: str, version: str) -> None:
        from clipos_api.agent_registry import AgentVersionRecord

        if (
            session.scalars(
                sa.select(AgentVersionRecord).where(
                    AgentVersionRecord.agent_id == agent_id,
                    AgentVersionRecord.version == version,
                )
            ).first()
            is None
        ):
            raise ValueError(f"agent version not found: {agent_id}:{version}")

    def grant(self, tool_id: str, agent_id: str, version: str) -> CapabilityGrant:
        """Grant an enabled tool to a registered agent version idempotently."""
        with self._sessions.begin() as session:
            tool = self._tool(session, tool_id)
            if tool is None:
                raise ValueError(f"tool not found: {tool_id}")
            self._require_agent_version(session, agent_id, version)
            row = self._grant_row(session, agent_id, version, tool_id)
            if row is None:
                row = CapabilityGrantRecord(
                    agent_id=agent_id,
                    version=version,
                    tool_id=tool_id,
                    active=True,
                    granted_at=datetime.now(UTC),
                )
                session.add(row)
                session.flush()
                stored = _grant_from_row(row)
            elif row.active:
                return _grant_from_row(row)
            else:
                row.active = True
                row.granted_at = datetime.now(UTC)
                row.revoked_at = None
                session.flush()
                stored = _grant_from_row(row)
        _audit(
            "tool_registry.granted",
            f"grant:{agent_id}:{version}:{tool_id}",
            {"tool_id": tool_id, "active": True},
        )
        return stored

    def revoke(self, tool_id: str, agent_id: str, version: str) -> CapabilityGrant:
        """Revoke one grant; revoking an inactive grant is a no-op."""
        with self._sessions.begin() as session:
            tool = self._tool(session, tool_id)
            if tool is None:
                raise ValueError(f"tool not found: {tool_id}")
            row = self._grant_row(session, agent_id, version, tool_id)
            if row is None:
                raise ValueError(f"grant not found: {agent_id}:{version}:{tool_id}")
            if not row.active:
                return _grant_from_row(row)
            row.active = False
            row.revoked_at = datetime.now(UTC)
            session.flush()
            stored = _grant_from_row(row)
        _audit(
            "tool_registry.revoked",
            f"grant:{agent_id}:{version}:{tool_id}",
            {"tool_id": tool_id, "active": False},
        )
        return stored

    def revalidate(self, tool_id: str | None = None) -> list[CapabilityGrant]:
        """Drop grants for disabled tools (optionally one tool) and audit each."""
        dropped: list[CapabilityGrant] = []
        with self._sessions.begin() as session:
            tool_query = sa.select(ToolRecordModel).where(ToolRecordModel.enabled.is_(False))
            if tool_id is not None:
                tool_query = tool_query.where(ToolRecordModel.tool_id == tool_id)
            tool_rows = session.scalars(tool_query).all()
            for tool_row in tool_rows:
                grants = session.scalars(
                    sa.select(CapabilityGrantRecord).where(
                        CapabilityGrantRecord.tool_id == tool_row.tool_id,
                        CapabilityGrantRecord.active.is_(True),
                    )
                ).all()
                now = datetime.now(UTC)
                for grant in grants:
                    grant.active = False
                    grant.revoked_at = now
                    session.flush()
                    stored = _grant_from_row(grant)
                    dropped.append(stored)
                    _audit(
                        "tool_registry.revoked",
                        f"grant:{stored.agent_id}:{stored.version}:{stored.tool_id}",
                        {"reason": "tool_disabled"},
                    )
        return dropped

    def list_grants(self, tool_id: str, active_only: bool = True) -> list[CapabilityGrant]:
        """Return grants for one tool, ordered by agent identity."""
        with self._sessions() as session:
            query = sa.select(CapabilityGrantRecord).where(CapabilityGrantRecord.tool_id == tool_id)
            if active_only:
                query = query.where(CapabilityGrantRecord.active.is_(True))
            rows = session.scalars(
                query.order_by(
                    CapabilityGrantRecord.agent_id,
                    CapabilityGrantRecord.version,
                )
            ).all()
            return [_grant_from_row(row) for row in rows]

    def check_capability(self, agent_id: str, version: str, tool_id: str) -> bool:
        """Return True only for an enabled, granted, and non-prohibited tool."""
        from clipos_api.agent_registry import AgentVersionRecord

        with self._sessions() as session:
            tool = self._tool(session, tool_id)
            if tool is None or not tool.enabled:
                return False
            agent = session.scalars(
                sa.select(AgentVersionRecord).where(
                    AgentVersionRecord.agent_id == agent_id,
                    AgentVersionRecord.version == version,
                )
            ).first()
            if agent is None:
                return False
            grant = self._grant_row(session, agent_id, version, tool_id)
            if grant is None or not grant.active:
                return False
            return tool_id not in agent.prohibited_tools


def _registry_store(engine: Annotated[Engine, Depends(database_engine)]) -> ToolRegistryStore:
    return ToolRegistryStore.from_engine(engine)


def _agents_store(engine: Annotated[Engine, Depends(database_engine)]) -> AgentRegistryStore:
    return AgentRegistryStore.from_engine(engine)


def register_tool(
    request: ToolDef,
    registry: Annotated[ToolRegistryStore, Depends(_registry_store)],
) -> ToolDef:
    """Register one tool (idempotent on matching definition)."""
    try:
        return registry.register(request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def get_tools(
    registry: Annotated[ToolRegistryStore, Depends(_registry_store)],
) -> list[ToolDef]:
    """Return the full tool registry for operator UIs."""
    return registry.list_tools()


def grant_tool(
    tool_id: str,
    request: CapabilityGrantRequest,
    registry: Annotated[ToolRegistryStore, Depends(_registry_store)],
) -> CapabilityGrant:
    """Grant one tool to an immutable agent version."""
    try:
        return registry.grant(tool_id, request.agent_id, request.version)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def revoke_tool(
    tool_id: str,
    request: CapabilityGrantRequest,
    registry: Annotated[ToolRegistryStore, Depends(_registry_store)],
) -> CapabilityGrant:
    """Revoke one agent-version tool grant."""
    try:
        return registry.revoke(tool_id, request.agent_id, request.version)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def get_tool_agents(
    tool_id: str,
    registry: Annotated[ToolRegistryStore, Depends(_registry_store)],
) -> list[CapabilityGrant]:
    """Return active grants for one tool."""
    return registry.list_grants(tool_id, active_only=True)


def revalidate_tool(
    tool_id: str,
    registry: Annotated[ToolRegistryStore, Depends(_registry_store)],
) -> list[CapabilityGrant]:
    """Drop active grants that point at a disabled tool."""
    return registry.revalidate(tool_id)


router = APIRouter(prefix="/tools", tags=["tool-registry"])
router.add_api_route("", get_tools, methods=["GET"], name="get_tools")
router.add_api_route("", register_tool, methods=["POST"], name="register_tool")
router.add_api_route("/{tool_id}/grant", grant_tool, methods=["POST"], name="grant_tool")
router.add_api_route("/{tool_id}/revoke", revoke_tool, methods=["POST"], name="revoke_tool")
router.add_api_route("/{tool_id}/agents", get_tool_agents, methods=["GET"], name="get_tool_agents")
router.add_api_route(
    "/{tool_id}/revalidate", revalidate_tool, methods=["POST"], name="revalidate_tool"
)
