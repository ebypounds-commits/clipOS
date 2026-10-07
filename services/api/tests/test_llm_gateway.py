"""Tests for the provider-neutral LLM gateway (CLIP-023)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit, llm_gateway, model_registry
from clipos_api.llm_gateway import (
    AdapterRegistry,
    ChatMessage,
    LLMGateway,
    LLMRequest,
    LLMResponse,
    ProviderAdapter,
    RequestParams,
)
from clipos_api.model_registry import (
    HealthState,
    ModelRegistryStore,
    ModelRoute,
)


class FakeAdapter(ProviderAdapter):
    """Deterministic adapter that records calls and optionally fails."""

    def __init__(self, provider: str, fail: bool = False) -> None:
        self.provider = provider
        self.fail = fail
        self.calls: list[LLMRequest] = []

    def send(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self.fail:
            raise RuntimeError(f"{self.provider} unavailable")
        return LLMResponse(
            content=f"answer from {self.provider}", usage={"input_tokens": 3, "output_tokens": 5}
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
    path = Path("/tmp") / "clipos-023-llm-gateway-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ModelRegistryStore:
    return ModelRegistryStore.from_engine(engine)


def _route(alias: str, provider: str, priority: int, **kwargs: Any) -> ModelRoute:
    return ModelRoute(
        alias=alias,
        provider=provider,
        endpoint_ref=f"https://{provider}.internal/infer",
        model_ref=f"ref-{provider}",
        priority=priority,
        metadata={"region": "eu-central"},
        **kwargs,
    )


def _request(alias: str = "reasoning-standard", execution_id: str = "exec-1") -> LLMRequest:
    return LLMRequest(
        alias=alias,
        messages=[ChatMessage(role="user", content="hello")],
        params=RequestParams(temperature=0.2, max_tokens=64),
        execution_id=execution_id,
    )


def _gateway(
    store: ModelRegistryStore, **adapters: ProviderAdapter
) -> tuple[LLMGateway, dict[str, FakeAdapter]]:
    gateway = LLMGateway(store, AdapterRegistry())
    fakes: dict[str, FakeAdapter] = {}
    for provider, adapter in adapters.items():
        gateway.register_adapter(provider, lambda route, a=adapter: a)
        fakes[provider] = adapter
    return gateway, fakes


def _events(audit_log: Path) -> list[dict[str, Any]]:
    import json

    return [
        json.loads(line)
        for line in audit_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_fallback_order_first_fails_second_wins_and_degrades(
    store: ModelRegistryStore, audit_log: Path
) -> None:
    store.register(_route("reasoning-standard", "primary", 1))
    store.register(_route("reasoning-standard", "backup", 2))
    gateway, _ = _gateway(
        store,
        primary=FakeAdapter("primary", fail=True),
        backup=FakeAdapter("backup"),
    )

    response = gateway.dispatch(_request())

    assert response.provider == "backup"
    assert response.model_ref == "ref-backup"
    assert response.alias == "reasoning-standard"
    assert response.execution_id == "exec-1"
    assert response.usage.input_tokens == 3
    assert response.usage.output_tokens == 5
    degraded = store.list_routes("reasoning-standard")
    assert degraded[0].health_state is HealthState.DEGRADED
    assert degraded[1].health_state is HealthState.OK
    events = _events(audit_log)
    assert any(
        e["action"] == "llm_gateway.dispatch_started" and e["subject"] == "execution:exec-1"
        for e in events
    )
    assert any(e["action"] == "llm_gateway.dispatch_succeeded" for e in events)
    assert any(e["action"] == "model_registry.health_changed" for e in events)


def test_disabled_routes_are_skipped(store: ModelRegistryStore, audit_log: Path) -> None:
    store.register(_route("reasoning-standard", "primary", 1))
    store.register(_route("reasoning-standard", "backup", 2))
    route = store.list_routes("reasoning-standard")[0]
    store.set_route_enabled(route.alias, route.provider, route.model_ref, False)
    gateway, fakes = _gateway(
        store,
        primary=FakeAdapter("primary"),
        backup=FakeAdapter("backup"),
    )

    response = gateway.dispatch(_request())

    assert response.provider == "backup"
    assert fakes["primary"].calls == []


def test_dispatch_is_idempotent_per_execution_id(
    store: ModelRegistryStore, audit_log: Path
) -> None:
    store.register(_route("reasoning-standard", "primary", 1))
    gateway, fakes = _gateway(store, primary=FakeAdapter("primary"))

    first = gateway.dispatch(_request(execution_id="same-exec"))
    second = gateway.dispatch(_request(execution_id="same-exec"))

    assert first == second
    assert len(fakes["primary"].calls) == 1


def test_unknown_provider_falls_through_to_next_route(
    store: ModelRegistryStore, audit_log: Path
) -> None:
    store.register(_route("reasoning-standard", "ghost", 1))
    store.register(_route("reasoning-standard", "backup", 2))
    gateway, fakes = _gateway(store, backup=FakeAdapter("backup"))

    response = gateway.dispatch(_request())

    assert response.provider == "backup"
    assert fakes["backup"].calls


def test_all_routes_failing_raises_llm_dispatch_error(
    store: ModelRegistryStore, audit_log: Path
) -> None:
    store.register(_route("reasoning-standard", "primary", 1))
    gateway, _ = _gateway(store, primary=FakeAdapter("primary", fail=True))

    with pytest.raises(llm_gateway.LLMDispatchError):
        gateway.dispatch(_request())
    events = _events(audit_log)
    assert any(e["action"] == "llm_gateway.dispatch_failed" for e in events)
    routes = store.list_routes("reasoning-standard")
    assert routes[0].health_state is HealthState.DEGRADED


def test_no_routes_for_alias_raises(store: ModelRegistryStore, audit_log: Path) -> None:
    gateway, _ = _gateway(store)
    with pytest.raises(llm_gateway.LLMDispatchError):
        gateway.dispatch(_request())


def test_no_provider_sdks_at_module_import() -> None:
    code = (
        "import sys, clipos_api.llm_gateway;"
        "print('openai' in sys.modules, 'anthropic' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False False"


def test_no_hard_coded_provider_model_ids_in_module() -> None:
    source = Path(llm_gateway.__file__).read_text(encoding="utf-8").lower()
    for marker in ("gpt-", "claude-", "gemini-", "llama-", "mistral-"):
        assert marker not in source


def test_gateway_owns_response_fields(store: ModelRegistryStore, audit_log: Path) -> None:
    store.register(_route("reasoning-standard", "primary", 1))

    class SpoofingAdapter(FakeAdapter):
        def send(self, request: LLMRequest) -> LLMResponse:
            base = super().send(request)
            return base.model_copy(
                update={
                    "alias": "spoofed",
                    "provider": "spoofed",
                    "model_ref": "spoofed",
                    "execution_id": "spoofed",
                }
            )

    gateway = LLMGateway(store, AdapterRegistry())
    adapter = SpoofingAdapter("primary")
    gateway.register_adapter("primary", lambda route: adapter)

    response = gateway.dispatch(_request())
    assert response.provider == "primary"
    assert response.model_ref == "ref-primary"
    assert response.alias == "reasoning-standard"
    assert response.execution_id == "exec-1"


def test_main_wiring_point_returns_gateway(engine: Engine, audit_log: Path) -> None:
    from clipos_api.main import build_llm_gateway

    gateway = build_llm_gateway(engine)
    gateway.register_adapter("primary", lambda route: FakeAdapter("primary"))
    assert isinstance(gateway, LLMGateway)
