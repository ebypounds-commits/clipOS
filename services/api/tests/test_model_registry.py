"""Tests for the model registry schema, routing, and audit (CLIP-022)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit, model_registry
from clipos_api.main import app
from clipos_api.model_registry import (
    HealthRequest,
    HealthState,
    ModelRegistryStore,
    ModelRoute,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    model_registry.Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-022-model-registry-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ModelRegistryStore:
    return ModelRegistryStore.from_engine(engine)


def _route(alias: str, provider: str, model_ref: str, priority: int) -> ModelRoute:
    return ModelRoute(
        alias=alias,
        provider=provider,
        endpoint_ref=f"https://{provider}.internal/infer",
        model_ref=model_ref,
        priority=priority,
        metadata={"region": "eu-central"},
    )


def test_register_is_idempotent_and_rejects_conflicts(store: ModelRegistryStore) -> None:
    route = _route("reasoning-standard", "route-a", "ref-a", 1)
    registered = store.register(route)
    assert registered.health_state is HealthState.OK
    assert store.register(route) == registered
    conflicting = route.model_copy(update={"priority": 9})
    with pytest.raises(ValueError, match="already registered"):
        store.register(conflicting)


def test_resolution_orders_by_priority_and_excludes_disabled(
    store: ModelRegistryStore,
) -> None:
    third = store.register(_route("reasoning-standard", "route-c", "ref-c", 3))
    second = store.register(_route("reasoning-standard", "route-b", "ref-b", 2))
    first = store.register(_route("reasoning-standard", "route-a", "ref-a", 1))
    disabled = store.register(_route("reasoning-standard", "route-d", "ref-d", 0))
    store.set_route_enabled("reasoning-standard", "route-d", "ref-d", enabled=False)
    degraded = store.register(_route("reasoning-standard", "route-e", "ref-e", 4))
    degraded = store.set_route_health(
        "reasoning-standard", "route-e", "ref-e", HealthState.DEGRADED
    )

    chain = store.resolve_alias("reasoning-standard")
    assert chain == [first, second, third, degraded]
    assert disabled not in chain
    with pytest.raises(ValueError, match="alias must be non-empty"):
        store.resolve_alias(" ")
    assert store.resolve_alias("missing-alias") == []


def test_health_and_enable_transitions_are_audited(
    store: ModelRegistryStore, audit_log: Path
) -> None:
    store.register(_route("reasoning-standard", "route-a", "ref-a", 1))
    degraded = store.set_route_health(
        "reasoning-standard", "route-a", "ref-a", HealthState.DEGRADED
    )
    assert degraded.health_state is HealthState.DEGRADED
    # same-state transition is a no-op without audit
    assert (
        store.set_route_health("reasoning-standard", "route-a", "ref-a", HealthState.DEGRADED)
        == degraded
    )
    ok = store.set_route_health("reasoning-standard", "route-a", "ref-a", HealthState.OK)
    assert ok.health_state is HealthState.OK
    events = [event["action"] for event in audit.read_events()]
    assert events == [
        "model_registry.route_registered",
        "model_registry.health_changed",
        "model_registry.health_changed",
    ]

    disabled = store.set_route_enabled("reasoning-standard", "route-a", "ref-a", False)
    assert disabled.enabled is False
    enabled = store.set_route_enabled("reasoning-standard", "route-a", "ref-a", True)
    assert enabled.enabled is True
    with pytest.raises(ValueError, match="not found"):
        store.set_route_health("missing-alias", "route-a", "ref-a", HealthState.OK)
    events = [event["action"] for event in audit.read_events()]
    assert "model_registry.route_disabled" in events
    assert "model_registry.route_enabled" in events


def test_registry_routes_are_wired() -> None:
    names = {route.name for route in app.routes if isinstance(route, APIRoute)}
    assert {
        "get_model_routes",
        "register_model_route",
        "get_model_alias_routes",
        "set_model_route_health",
        "resolve_model_alias",
    } <= names


def test_no_provider_model_identifiers_in_registry_module() -> None:
    src = Path(model_registry.__file__).read_text()
    for provider_token in ("gpt-", "claude-", "gemini-", "deepseek", "qwen"):
        assert provider_token not in src, provider_token


def test_health_request_contract_accepts_enum_only_states() -> None:
    request = HealthRequest(provider="route-a", model_ref="ref-a", health_state=HealthState.OK)
    assert request.health_state is HealthState.OK
