"""Plugin/Connector Registry: four governed plugin types and audits.

Plugins are registered by natural key ``plugin_id`` with immutable definitions
and audited enable/disable/health transitions.  Operator-installed extensions
carry an additional allow-list gate: they can only be enabled once they have
been marked allow-listed.  Secret material is always referenced by ref and
inline credentials are rejected at registration time.
"""

from __future__ import annotations

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
from clipos_api.agent_registry import database_engine
from clipos_api.repository import Base

_PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_DEFAULT_ACTOR = "registry-operator"
# Bare opaque credentials and URLs with embedded userinfo are never refs.
_BARE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_CREDENTIAL_URL_PATTERN = re.compile(r"://[^/\s:@]+:[^@\s]+@")


class PluginType(str, Enum):
    """The four governed plugin types from the registry spec."""

    NATIVE_ADAPTER = "native_adapter"
    MCP_SERVER = "mcp_server"
    REST_WEBHOOK = "rest_webhook"
    OPERATOR_EXTENSION = "operator_extension"


class HealthState(str, Enum):
    """Operational health states for one registered plugin."""

    OK = "ok"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class PluginDef(BaseModel):
    """Immutable definition contract for one governed plugin."""

    model_config = ConfigDict(frozen=True)

    plugin_id: str = Field(min_length=1, max_length=255, pattern=_PLUGIN_ID_PATTERN.pattern)
    plugin_type: PluginType
    name: str = Field(min_length=1, max_length=255)
    config_ref: str = Field(min_length=1, max_length=512)
    allow_listed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("config_ref")
    @classmethod
    def _reject_inline_secrets(cls, value: str) -> str:
        if _CREDENTIAL_URL_PATTERN.search(value) or _BARE_TOKEN_PATTERN.fullmatch(value):
            raise ValueError(
                "config_ref must be a secret reference, not inline credential material"
            )
        return value


class PluginRecordModel(Base):
    """SQL row for one registered plugin."""

    __tablename__ = "plugins"
    __table_args__ = (sa.Index("uq_plugins_plugin_id", "plugin_id", unique=True),)

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    plugin_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    plugin_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    config_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    health_state: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    allow_listed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    plugin_metadata: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    """Normalize values read from backends that strip the timezone (SQLite)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _plugin_from_row(row: PluginRecordModel) -> PluginDef:
    """Return the immutable definition portion of one stored plugin row."""
    return PluginDef.model_validate(
        {
            "plugin_id": row.plugin_id,
            "plugin_type": row.plugin_type,
            "name": row.name,
            "config_ref": row.config_ref,
            "allow_listed": row.allow_listed,
            "metadata": row.plugin_metadata,
        }
    )


def _plugin_state(row: PluginRecordModel) -> PluginState:
    return PluginState(
        plugin=_plugin_from_row(row),
        health_state=HealthState(row.health_state),
        enabled=row.enabled,
    )


class PluginState(BaseModel):
    """Stored plugin definition plus its mutable operational state."""

    model_config = ConfigDict(frozen=True)

    plugin: PluginDef
    health_state: HealthState
    enabled: bool


def _definition_signature(plugin: PluginDef) -> dict[str, Any]:
    """All PluginDef fields are immutable; metadata included."""
    return plugin.model_dump(mode="json")


def _audit(action: str, subject: str, details: dict[str, Any]) -> dict[str, Any]:
    return audit.record(action=action, actor=_DEFAULT_ACTOR, subject=subject, details=details)


class PluginRegistryStore:
    """Session-bound SQL persistence for governed plugins."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> PluginRegistryStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    @staticmethod
    def _plugin(session: Session, plugin_id: str) -> PluginRecordModel | None:
        return session.scalars(
            sa.select(PluginRecordModel).where(PluginRecordModel.plugin_id == plugin_id)
        ).first()

    def register_in_transaction(self, session: Session, plugin: PluginDef) -> PluginState:
        """Idempotently register one plugin; conflicting immutable fields fail."""
        now = datetime.now(UTC)
        row = self._plugin(session, plugin.plugin_id)
        if row is not None:
            stored = _plugin_from_row(row)
            if _definition_signature(stored) != _definition_signature(plugin):
                raise ValueError(
                    f"plugin already registered with a different definition: {plugin.plugin_id}"
                )
            return _plugin_state(row)
        initial_health = HealthState.DISABLED if not plugin.allow_listed else HealthState.OK
        if plugin.plugin_type is not PluginType.OPERATOR_EXTENSION:
            initial_health = HealthState.OK
        row = PluginRecordModel(
            plugin_id=plugin.plugin_id,
            plugin_type=plugin.plugin_type.value,
            name=plugin.name,
            config_ref=plugin.config_ref,
            health_state=initial_health.value,
            enabled=False,
            allow_listed=plugin.allow_listed,
            plugin_metadata=plugin.metadata,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        _audit(
            "plugin_registry.registered",
            f"plugin:{plugin.plugin_id}",
            {
                "plugin_type": plugin.plugin_type.value,
                "config_ref": plugin.config_ref,
                "allow_listed": plugin.allow_listed,
            },
        )
        return _plugin_state(row)

    def register(self, plugin: PluginDef) -> PluginState:
        """Register one plugin; a matching re-registration is a no-op."""
        with self._sessions.begin() as session:
            return self.register_in_transaction(session, plugin)

    def list_plugins(self) -> list[PluginState]:
        """Return every registered plugin ordered by stable plugin_id."""
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(PluginRecordModel).order_by(PluginRecordModel.plugin_id)
            ).all()
            return [_plugin_state(row) for row in rows]

    def get_plugin(self, plugin_id: str) -> PluginState | None:
        """Return one plugin, or None when it is not registered."""
        with self._sessions() as session:
            row = self._plugin(session, plugin_id)
            return _plugin_state(row) if row is not None else None

    def set_enabled(self, plugin_id: str, enabled: bool) -> PluginState:
        """Enable/disable one plugin, enforcing the extension allow-list gate."""
        if enabled:
            action = "plugin_registry.enabled"
        else:
            action = "plugin_registry.disabled"
        with self._sessions.begin() as session:
            row = self._plugin(session, plugin_id)
            if row is None:
                raise ValueError(f"plugin not found: {plugin_id}")
            if (
                enabled
                and row.plugin_type == PluginType.OPERATOR_EXTENSION.value
                and not row.allow_listed
            ):
                _audit(
                    "plugin_registry.extension_denied",
                    f"plugin:{plugin_id}",
                    {"reason": "extension_not_allow_listed"},
                )
                raise ValueError(f"extension plugin is not allow-listed: {plugin_id}")
            if row.enabled == enabled:
                return _plugin_state(row)
            row.enabled = enabled
            row.updated_at = datetime.now(UTC)
            if not enabled:
                row.health_state = HealthState.DISABLED.value
            elif row.health_state == HealthState.DISABLED.value:
                row.health_state = HealthState.OK.value
            session.flush()
        _audit(action, f"plugin:{plugin_id}", {"enabled": enabled})
        return _plugin_state(row)

    def set_health(self, plugin_id: str, state: HealthState) -> PluginState:
        """Record one audited health transition for a registered plugin."""
        with self._sessions.begin() as session:
            row = self._plugin(session, plugin_id)
            if row is None:
                raise ValueError(f"plugin not found: {plugin_id}")
            if row.health_state == state.value:
                return _plugin_state(row)
            row.health_state = state.value
            row.updated_at = datetime.now(UTC)
            session.flush()
        _audit(
            "plugin_registry.health_changed",
            f"plugin:{plugin_id}",
            {"health_state": state.value},
        )
        return _plugin_state(row)


def _registry_store(engine: Annotated[Engine, Depends(database_engine)]) -> PluginRegistryStore:
    return PluginRegistryStore.from_engine(engine)


class HealthRequest(BaseModel):
    """Health-state update payload."""

    model_config = ConfigDict(frozen=True)

    health_state: HealthState


def register_plugin(
    request: PluginDef,
    registry: Annotated[PluginRegistryStore, Depends(_registry_store)],
) -> PluginState:
    """Register one plugin (idempotent on matching definition)."""
    try:
        return registry.register(request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def get_plugins(
    registry: Annotated[PluginRegistryStore, Depends(_registry_store)],
) -> list[PluginState]:
    """Return the full plugin registry for operator UIs."""
    return registry.list_plugins()


def get_plugin(
    plugin_id: str,
    registry: Annotated[PluginRegistryStore, Depends(_registry_store)],
) -> PluginState:
    """Return one registered plugin."""
    plugin = registry.get_plugin(plugin_id)
    if plugin is None:
        raise HTTPException(status_code=404, detail=f"plugin not found: {plugin_id}")
    return plugin


def _set_enabled(plugin_id: str, enabled: bool, registry: PluginRegistryStore) -> PluginState:
    try:
        return registry.set_enabled(plugin_id, enabled)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def enable_plugin(
    plugin_id: str,
    registry: Annotated[PluginRegistryStore, Depends(_registry_store)],
) -> PluginState:
    """Enable one registered plugin subject to the extension allow-list gate."""
    return _set_enabled(plugin_id, True, registry)


def disable_plugin(
    plugin_id: str,
    registry: Annotated[PluginRegistryStore, Depends(_registry_store)],
) -> PluginState:
    """Disable one registered plugin."""
    return _set_enabled(plugin_id, False, registry)


def set_plugin_health(
    plugin_id: str,
    request: HealthRequest,
    registry: Annotated[PluginRegistryStore, Depends(_registry_store)],
) -> PluginState:
    """Record one health transition for a registered plugin."""
    try:
        return registry.set_health(plugin_id, request.health_state)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


router = APIRouter(prefix="/plugins", tags=["plugin-registry"])
router.add_api_route("", get_plugins, methods=["GET"], name="get_plugins")
router.add_api_route("", register_plugin, methods=["POST"], name="register_plugin")
router.add_api_route("/{plugin_id}", get_plugin, methods=["GET"], name="get_plugin")
router.add_api_route("/{plugin_id}/enable", enable_plugin, methods=["POST"], name="enable_plugin")
router.add_api_route(
    "/{plugin_id}/disable", disable_plugin, methods=["POST"], name="disable_plugin"
)
router.add_api_route(
    "/{plugin_id}/health", set_plugin_health, methods=["POST"], name="set_plugin_health"
)
