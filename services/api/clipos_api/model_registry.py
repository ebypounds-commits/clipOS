"""Model Registry: logical aliases mapped to ordered provider routes.

Routes are persisted by their natural key ``(alias, provider, model_ref)`` and
re-registration is idempotent.  Resolution returns only enabled routes in
priority order so the LLM gateway can walk the fallback chain.  Material state
changes (registration, health, enable/disable) emit operator-visible audits.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.agent_registry import database_engine
from clipos_api.repository import Base

_LOGICAL_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_DEFAULT_ACTOR = "registry-operator"


class HealthState(str, Enum):
    """Operator-visible health of one provider route."""

    OK = "ok"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class ModelRoute(BaseModel):
    """One ordered, database-configured route for a logical model alias."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    alias: str = Field(min_length=1, max_length=255, pattern=_LOGICAL_ALIAS_PATTERN.pattern)
    provider: str = Field(min_length=1, max_length=255)
    endpoint_ref: str = Field(min_length=1, max_length=255)
    model_ref: str = Field(min_length=1, max_length=255)
    priority: int = Field(ge=0)
    enabled: bool = True
    health_state: HealthState = HealthState.OK
    metadata: dict[str, Any] = Field(default_factory=dict)


class HealthRequest(BaseModel):
    """POST payload for one route health transition."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    provider: str = Field(min_length=1, max_length=255)
    model_ref: str = Field(min_length=1, max_length=255)
    health_state: HealthState


class ModelRouteRecord(Base):
    """SQL row for one provider route behind a logical alias."""

    __tablename__ = "model_routes"
    __table_args__ = (
        sa.Index(
            "uq_model_routes_identity",
            "alias",
            "provider",
            "model_ref",
            unique=True,
        ),
        sa.Index("ix_model_routes_alias_priority", "alias", "priority"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    endpoint_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    model_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    health_state: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    route_metadata: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _route_from_row(row: ModelRouteRecord) -> ModelRoute:
    return ModelRoute(
        alias=row.alias,
        provider=row.provider,
        endpoint_ref=row.endpoint_ref,
        model_ref=row.model_ref,
        priority=row.priority,
        enabled=row.enabled,
        health_state=HealthState(row.health_state),
        metadata=row.route_metadata,
    )


def _route_signature(route: ModelRoute) -> dict[str, Any]:
    """Exclude mutable health/enable state from route-identity comparisons."""
    data = route.model_dump()
    data.pop("health_state")
    data.pop("enabled")
    return data


def _audit(action: str, actor: str, subject: str, details: dict[str, Any]) -> dict[str, Any]:
    return audit.record(action=action, actor=actor, subject=subject, details=details)


class ModelRegistryStore:
    """Session-bound SQL persistence for the model route registry."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ModelRegistryStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    @staticmethod
    def _row(session: Session, route: ModelRoute) -> ModelRouteRecord | None:
        return session.scalars(
            sa.select(ModelRouteRecord).where(
                ModelRouteRecord.alias == route.alias,
                ModelRouteRecord.provider == route.provider,
                ModelRouteRecord.model_ref == route.model_ref,
            )
        ).first()

    def register_in_transaction(self, session: Session, route: ModelRoute) -> ModelRoute:
        """Idempotently register one route; a matching re-registration is a no-op."""
        now = datetime.now(UTC)
        row = self._row(session, route)
        if row is not None:
            stored = _route_from_row(row)
            if _route_signature(stored) != _route_signature(route):
                raise ValueError(
                    "route already registered with a different definition: "
                    f"{route.alias}:{route.provider}:{route.model_ref}"
                )
            return stored
        row = ModelRouteRecord(
            alias=route.alias,
            provider=route.provider,
            endpoint_ref=route.endpoint_ref,
            model_ref=route.model_ref,
            priority=route.priority,
            enabled=route.enabled,
            health_state=route.health_state.value,
            route_metadata=route.metadata,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        _audit(
            "model_registry.route_registered",
            _DEFAULT_ACTOR,
            f"model_alias:{route.alias}",
            {
                "provider": route.provider,
                "model_ref": route.model_ref,
                "priority": route.priority,
                "enabled": route.enabled,
            },
        )
        return _route_from_row(row)

    def register(self, route: ModelRoute) -> ModelRoute:
        """Register one route; a matching re-registration is a no-op."""
        with self._sessions.begin() as session:
            return self.register_in_transaction(session, route)

    def _resolved_row(
        self, session: Session, alias: str, provider: str, model_ref: str
    ) -> ModelRouteRecord:
        row = session.scalars(
            sa.select(ModelRouteRecord).where(
                ModelRouteRecord.alias == alias,
                ModelRouteRecord.provider == provider,
                ModelRouteRecord.model_ref == model_ref,
            )
        ).first()
        if row is None:
            raise ValueError(f"model route not found: {alias}:{provider}:{model_ref}")
        return row

    def set_route_health(
        self,
        alias: str,
        provider: str,
        model_ref: str,
        health_state: HealthState,
    ) -> ModelRoute:
        """Transition one route's health; a no-op transition is not audited."""
        with self._sessions.begin() as session:
            row = self._resolved_row(session, alias, provider, model_ref)
            previous = HealthState(row.health_state)
            if previous == health_state:
                return _route_from_row(row)
            row.health_state = health_state.value
            row.updated_at = datetime.now(UTC)
            session.flush()
        _audit(
            "model_registry.health_changed",
            _DEFAULT_ACTOR,
            f"model_route:{alias}:{provider}:{model_ref}",
            {"from": previous.value, "to": health_state.value},
        )
        return _route_from_row(row)

    def set_route_enabled(
        self,
        alias: str,
        provider: str,
        model_ref: str,
        enabled: bool,
    ) -> ModelRoute:
        """Enable or disable one route; a no-op transition is not audited."""
        action = "model_registry.route_enabled" if enabled else "model_registry.route_disabled"
        with self._sessions.begin() as session:
            row = self._resolved_row(session, alias, provider, model_ref)
            if row.enabled == enabled:
                return _route_from_row(row)
            row.enabled = enabled
            if enabled and HealthState(row.health_state) is HealthState.DISABLED:
                row.health_state = HealthState.OK.value
            row.updated_at = datetime.now(UTC)
            session.flush()
        _audit(
            action,
            _DEFAULT_ACTOR,
            f"model_route:{alias}:{provider}:{model_ref}",
            {"enabled": enabled},
        )
        return _route_from_row(row)

    def resolve_alias(self, alias: str) -> list[ModelRoute]:
        """Return enabled, non-disabled-health routes in priority order."""
        if not alias.strip():
            raise ValueError("alias must be non-empty")
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ModelRouteRecord)
                .where(ModelRouteRecord.alias == alias)
                .where(ModelRouteRecord.enabled.is_(True))
                .where(ModelRouteRecord.health_state != HealthState.DISABLED.value)
                .order_by(
                    ModelRouteRecord.priority, ModelRouteRecord.provider, ModelRouteRecord.model_ref
                )
            ).all()
            return [_route_from_row(row) for row in rows]

    def list_routes(self, alias: str | None = None) -> list[ModelRoute]:
        """Return all routes, optionally filtered to one alias."""
        with self._sessions() as session:
            query = sa.select(ModelRouteRecord)
            if alias is not None:
                query = query.where(ModelRouteRecord.alias == alias)
            rows = session.scalars(
                query.order_by(
                    ModelRouteRecord.alias,
                    ModelRouteRecord.priority,
                    ModelRouteRecord.provider,
                    ModelRouteRecord.model_ref,
                )
            ).all()
            return [_route_from_row(row) for row in rows]


def _registry_store(engine: Annotated[Engine, Depends(database_engine)]) -> ModelRegistryStore:
    return ModelRegistryStore.from_engine(engine)


def register_route(
    request: ModelRoute,
    registry: Annotated[ModelRegistryStore, Depends(_registry_store)],
) -> ModelRoute:
    """Register one logical-alias route (idempotent on matching definition)."""
    try:
        return registry.register(request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def get_routes(
    registry: Annotated[ModelRegistryStore, Depends(_registry_store)],
) -> list[ModelRoute]:
    """Return the full model route registry for operator UIs."""
    return registry.list_routes()


def get_alias_routes(
    alias: str,
    registry: Annotated[ModelRegistryStore, Depends(_registry_store)],
) -> list[ModelRoute]:
    """Return every configured route for one logical alias."""
    return registry.list_routes(alias)


def set_route_health(
    alias: str,
    request: HealthRequest,
    registry: Annotated[ModelRegistryStore, Depends(_registry_store)],
) -> ModelRoute:
    """Transition one route's operator-visible health state."""
    try:
        return registry.set_route_health(
            alias, request.provider, request.model_ref, request.health_state
        )
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def resolve_alias_route(
    alias: str, registry: Annotated[ModelRegistryStore, Depends(_registry_store)]
) -> list[ModelRoute]:
    """Return the ordered enabled fallback chain for a logical alias."""
    try:
        return registry.resolve_alias(alias)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


router = APIRouter(prefix="/models", tags=["model-registry"])
router.add_api_route("", get_routes, methods=["GET"], name="get_model_routes")
router.add_api_route("", register_route, methods=["POST"], name="register_model_route")
router.add_api_route("/{alias}", get_alias_routes, methods=["GET"], name="get_model_alias_routes")
router.add_api_route(
    "/{alias}/health", set_route_health, methods=["POST"], name="set_model_route_health"
)
router.add_api_route(
    "/{alias}/resolve", resolve_alias_route, methods=["GET"], name="resolve_model_alias"
)
