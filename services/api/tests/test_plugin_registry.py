"""Tests for the plugin/connector registry (CLIP-027)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit, plugin_registry
from clipos_api.main import app
from clipos_api.plugin_registry import (
    HealthState,
    PluginDef,
    PluginRegistryStore,
    PluginType,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    plugin_registry.Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-027-plugin-registry-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> PluginRegistryStore:
    return PluginRegistryStore.from_engine(engine)


def _plugin(plugin_id: str = "youtube-publisher", **changes: object) -> PluginDef:
    values = {
        "plugin_id": plugin_id,
        "plugin_type": PluginType.NATIVE_ADAPTER,
        "name": "YouTube Publisher",
        "config_ref": "secrets/youtube/oauth",
        "allow_listed": False,
        "metadata": {"timeout_seconds": 10},
    }
    values.update(changes)
    return PluginDef.model_validate(values)


def _extension(plugin_id: str = "custom-ext", allow_listed: bool = False) -> PluginDef:
    return _plugin(
        plugin_id=plugin_id,
        plugin_type=PluginType.OPERATOR_EXTENSION,
        name="Custom Extension",
        config_ref="secrets/extensions/custom",
        allow_listed=allow_listed,
    )


def test_register_is_idempotent_and_rejects_conflicts(store: PluginRegistryStore) -> None:
    registered = store.register(_plugin())
    assert registered.enabled is False
    assert registered.health_state is HealthState.OK
    assert store.register(_plugin()) == registered
    with pytest.raises(ValueError, match="already registered"):
        store.register(_plugin(name="Different Name"))
    with pytest.raises(ValueError, match="plugin already registered"):
        store.register(_plugin(config_ref="secrets/youtube/oauth-v2"))


def test_extension_allow_list_gate(store: PluginRegistryStore, audit_log: Path) -> None:
    store.register(_extension("unlisted-ext"))
    with pytest.raises(ValueError, match="not allow-listed"):
        store.set_enabled("unlisted-ext", True)
    events = audit_log.read_text(encoding="utf-8")
    assert events.count("plugin_registry.extension_denied") == 1

    listed = store.register(_extension("listed-ext", allow_listed=True))
    assert listed.plugin.allow_listed is True
    enabled = store.set_enabled("listed-ext", True)
    assert enabled.enabled is True
    assert enabled.health_state is HealthState.OK


def test_native_adapter_enables_without_allow_list(store: PluginRegistryStore) -> None:
    store.register(_plugin())
    enabled = store.set_enabled("youtube-publisher", True)
    assert enabled.enabled is True
    assert enabled.health_state is HealthState.OK


def test_mcp_and_rest_webhook_types_enable_without_allow_list(
    store: PluginRegistryStore,
) -> None:
    store.register(
        _plugin(
            plugin_id="internal-mcp",
            plugin_type=PluginType.MCP_SERVER,
            name="Internal MCP",
        )
    )
    store.register(
        _plugin(
            plugin_id="notify-hook",
            plugin_type=PluginType.REST_WEBHOOK,
            name="Notify Webhook",
        )
    )
    assert store.set_enabled("internal-mcp", True).enabled is True
    assert store.set_enabled("notify-hook", True).enabled is True


def test_enable_disable_and_health_are_audited(store: PluginRegistryStore, audit_log: Path) -> None:
    store.register(_plugin())
    store.set_enabled("youtube-publisher", True)
    store.set_health("youtube-publisher", HealthState.DEGRADED)
    store.set_enabled("youtube-publisher", False)
    disabled = store.get_plugin("youtube-publisher")
    assert disabled is not None
    assert disabled.enabled is False
    assert disabled.health_state is HealthState.DISABLED
    events = audit_log.read_text(encoding="utf-8")
    assert events.count("plugin_registry.registered") == 1
    assert events.count("plugin_registry.enabled") == 1
    assert events.count("plugin_registry.disabled") == 1
    assert events.count("plugin_registry.health_changed") == 1
    assert '"degraded"' in events


def test_config_ref_rejects_inline_secret_material() -> None:
    with pytest.raises(ValueError, match="secret reference"):
        PluginDef.model_validate(
            {
                "plugin_id": "leaky",
                "plugin_type": PluginType.REST_WEBHOOK,
                "name": "Leaky",
                "config_ref": "https://user:hunter2@example.com/api",
            }
        )
    with pytest.raises(ValueError, match="secret reference"):
        PluginDef.model_validate(
            {
                "plugin_id": "leaky-token",
                "plugin_type": PluginType.REST_WEBHOOK,
                "name": "Leaky Token",
                "config_ref": "inline-secret-material-example",
            }
        )
    assert (
        PluginDef.model_validate(
            {
                "plugin_id": "fine",
                "plugin_type": PluginType.REST_WEBHOOK,
                "name": "Fine",
                "config_ref": "secrets/webhooks/notify/token",
            }
        ).config_ref
        == "secrets/webhooks/notify/token"
    )


def test_plugin_routes_are_wired() -> None:
    names = {route.name for route in app.routes if isinstance(route, APIRoute)}
    assert {
        "get_plugins",
        "register_plugin",
        "get_plugin",
        "enable_plugin",
        "disable_plugin",
        "set_plugin_health",
    } <= names


def test_no_provider_model_ids_in_registry_contract() -> None:
    """Plugin identity is type-based; no provider/model ids belong in the contract."""
    fields = set(PluginDef.model_fields)
    assert not any("model" in name or "provider" in name for name in fields)
