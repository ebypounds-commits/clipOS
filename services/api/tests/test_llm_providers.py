"""Tests for provider adapters and the HTTP dispatch endpoint (Plan 5)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.llm_gateway import (
    AdapterRegistry,
    ChatMessage,
    LLMGateway,
    LLMRequest,
    LLMResponse,
    ProviderAdapter,
    RequestParams,
    Usage,
)
from clipos_api.llm_providers import (
    AnthropicAdapter,
    GeminiAdapter,
    OpenAICompatAdapter,
    ProviderDispatchError,
    build_provider_adapter,
)
from clipos_api.main import app
from clipos_api.model_registry import (
    ModelRegistryStore,
    ModelRoute,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-llmproviders-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


def _request() -> LLMRequest:
    return LLMRequest(
        alias="chat",
        messages=[
            ChatMessage(role="system", content="Be terse."),
            ChatMessage(role="user", content="Ping"),
        ],
        params=RequestParams(temperature=0.2, max_tokens=64),
        execution_id="exec-1",
    )


def _route(
    provider: str,
    endpoint: str = "https://wire.example/v1",
    metadata: dict | None = None,
) -> ModelRoute:
    return ModelRoute(
        alias="chat",
        provider=provider,
        endpoint_ref=endpoint,
        model_ref=f"{provider}-model",
        priority=0,
        metadata=metadata
        if metadata is not None
        else {"protocol": "compat", "api_key_env": "TEST_API_KEY"},
    )


# ---------------------------------------------------------------------------
# OpenAI-wire protocol
# ---------------------------------------------------------------------------


def test_openai_wire_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            },
        )

    adapter = OpenAICompatAdapter(
        "https://wire.example/v1",
        "OPENAI_API_KEY",
        "gpt-test",
        client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw),
    )
    response = adapter.send(_request())
    assert response.content == "pong"
    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 2
    assert captured[0].url.path.endswith("/chat/completions")
    assert captured[0].headers["authorization"] == "Bearer test-key"
    body = json.loads(captured[0].content)
    assert body["model"] == "gpt-test"
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 64
    assert body["messages"][0]["role"] == "system"


def test_openai_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    adapter = OpenAICompatAdapter("https://wire.example/v1", "OPENAI_API_KEY", "m")
    with pytest.raises(ProviderDispatchError, match="OPENAI_API_KEY"):
        adapter.send(_request())


# ---------------------------------------------------------------------------
# Anthropic (claude) protocol
# ---------------------------------------------------------------------------


def test_anthropic_wire_and_system_hoist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "claude-key")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "hey"}, {"type": "text", "text": " there"}],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )

    adapter = AnthropicAdapter(
        "https://wire.example/v1",
        "ANTHROPIC_API_KEY",
        "claude-test",
        client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw),
    )
    response = adapter.send(_request())
    assert response.content == "hey there"
    assert response.usage.input_tokens == 5
    request = captured[0]
    assert request.url.path.endswith("/messages")
    assert request.headers["x-api-key"] == "claude-key"
    assert "anthropic-version" in request.headers
    body = json.loads(request.content)
    assert body["system"] == "Be terse."
    assert all(m["role"] in ("user", "assistant") for m in body["messages"]) is True
    assert body["model"] == "claude-test"


# ---------------------------------------------------------------------------
# Gemini protocol
# ---------------------------------------------------------------------------


def test_gemini_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "hi from gemini"}]}}],
                "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4},
            },
        )

    adapter = GeminiAdapter(
        "https://generativelanguage.example/v1beta",
        "GEMINI_API_KEY",
        "gemini-test",
        client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw),
    )
    response = adapter.send(_request())
    assert response.content == "hi from gemini"
    assert response.usage.input_tokens == 9
    request = captured[0]
    assert ":generateContent" in request.url.path
    assert "gemini-test" in request.url.path
    assert request.headers["x-goog-api-key"] == "gemini-key"
    body = json.loads(request.content)
    assert "systemInstruction" in body
    assert body["contents"][0]["role"] == "user"


# ---------------------------------------------------------------------------
# Factory + registration
# ---------------------------------------------------------------------------


def test_factory_protocol_and_endpoint_override() -> None:
    route = _route("bench", endpoint="https://alt.example/v1")
    adapter = build_provider_adapter(route)
    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter._endpoint == "https://alt.example/v1"

    messages = build_provider_adapter(
        _route("bench", metadata={"protocol": "messages", "api_key_env": "K"})
    )
    assert isinstance(messages, AnthropicAdapter)
    gen = build_provider_adapter(
        _route("bench", metadata={"protocol": "gen-content", "api_key_env": "K"})
    )
    assert isinstance(gen, GeminiAdapter)


def test_factory_endpoint_from_provider_base_url_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WIDGET_BASE_URL", raising=False)
    with pytest.raises(ProviderDispatchError, match="WIDGET_BASE_URL"):
        build_provider_adapter(_route("widget", endpoint="plain-ref"))
    monkeypatch.setenv("WIDGET_BASE_URL", "https://widget.example/v1")
    adapter = build_provider_adapter(_route("widget", endpoint="plain-ref"))
    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter._endpoint == "https://widget.example/v1"


def test_factory_requires_api_key_env_metadata() -> None:
    route = _route("bench", metadata={"protocol": "compat"})
    with pytest.raises(ProviderDispatchError, match="api_key_env"):
        build_provider_adapter(route)


def test_gateway_registers_providers_from_registry(engine: Engine) -> None:
    from clipos_api.main import build_llm_gateway

    store = ModelRegistryStore.from_engine(engine)
    store.register(
        ModelRoute(
            alias="alpha",
            provider="alpha",
            endpoint_ref="https://a.example/v1",
            model_ref="a-1",
            priority=0,
            metadata={"protocol": "compat", "api_key_env": "A_KEY"},
        )
    )
    store.register(
        ModelRoute(
            alias="beta",
            provider="beta",
            endpoint_ref="https://b.example/v1",
            model_ref="b-1",
            priority=0,
            metadata={"protocol": "messages", "api_key_env": "B_KEY"},
        )
    )
    assert store.list_providers() == ["alpha", "beta"]
    gateway = build_llm_gateway(engine)
    alpha_route = _route("alpha", metadata={"api_key_env": "A_KEY"})
    beta_route = _route("beta", metadata={"protocol": "messages", "api_key_env": "B_KEY"})
    assert isinstance(
        gateway._adapters.adapter_for(alpha_route),
        OpenAICompatAdapter,
    )
    assert isinstance(
        gateway._adapters.adapter_for(beta_route),
        AnthropicAdapter,
    )


# ---------------------------------------------------------------------------
# HTTP dispatch endpoint
# ---------------------------------------------------------------------------


class _FakeAdapter(ProviderAdapter):
    def send(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(content="pong", usage=Usage(input_tokens=1, output_tokens=1))


class _FailingAdapter(ProviderAdapter):
    def send(self, request: LLMRequest) -> LLMResponse:
        raise ProviderDispatchError("upstream down")


def _seed_route(engine: Engine) -> None:
    store = ModelRegistryStore.from_engine(engine)
    store.register(
        ModelRoute(
            alias="chat",
            provider="claude",
            endpoint_ref="https://wire.example/v1",
            model_ref="claude-test",
            priority=0,
        )
    )


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    import clipos_api.llm_providers as lp
    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)

    def _fake_gateway(_engine):
        gw = LLMGateway(
            ModelRegistryStore.from_engine(_engine),
            AdapterRegistry(),
        )
        gw.register_adapter("claude", lambda route: _FakeAdapter())
        return gw

    monkeypatch.setattr(lp, "_gateway", _fake_gateway)
    return TestClient(app)


def test_dispatch_route_registered() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/models/{alias}/dispatch" in paths


def test_dispatch_success(client, engine: Engine) -> None:
    _seed_route(engine)
    resp = client.post(
        "/models/chat/dispatch",
        json={
            "execution_id": "dispatch-1",
            "messages": [{"role": "user", "content": "Ping"}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == "pong"
    assert body["alias"] == "chat"
    assert body["provider"] == "claude"
    assert body["model_ref"] == "claude-test"
    assert body["execution_id"] == "dispatch-1"


def test_dispatch_unknown_alias_404(client) -> None:
    resp = client.post(
        "/models/no-such-alias/dispatch",
        json={"execution_id": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 404


def test_dispatch_all_routes_fail_502(client, engine: Engine, monkeypatch):
    import clipos_api.llm_providers as lp

    _seed_route(engine)

    def _broken_gateway(_engine):
        gw = LLMGateway(
            ModelRegistryStore.from_engine(_engine),
            AdapterRegistry(),
        )
        gw.register_adapter("claude", lambda route: _FailingAdapter())
        return gw

    monkeypatch.setattr(lp, "_gateway", _broken_gateway)
    resp = client.post(
        "/models/chat/dispatch",
        json={"execution_id": "dispatch-2", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 502
    assert "dispatch failed" in resp.json()["detail"]


def test_dispatch_missing_key_env_named_in_detail(
    client,
    engine: Engine,
    monkeypatch,
) -> None:
    """502 names the unset env var (env name only, never a value)."""
    import clipos_api.llm_providers as lp

    store = ModelRegistryStore.from_engine(engine)
    store.register(
        ModelRoute(
            alias="chat",
            provider="claude",
            endpoint_ref="https://wire.example/v1",
            model_ref="claude-test",
            priority=0,
            metadata={"protocol": "messages", "api_key_env": "NEVER_SET_KEY_XYZ"},
        )
    )
    monkeypatch.delenv("NEVER_SET_KEY_XYZ", raising=False)

    def _broken_gateway(_engine):
        gw = LLMGateway(
            ModelRegistryStore.from_engine(_engine),
            AdapterRegistry(),
        )
        gw.register_adapter("claude", lambda route: _FailingAdapter())
        return gw

    monkeypatch.setattr(lp, "_gateway", _broken_gateway)
    resp = client.post(
        "/models/chat/dispatch",
        json={"execution_id": "dispatch-3", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "missing api key env: NEVER_SET_KEY_XYZ" in detail
    assert "NEVER_SET_KEY_XYZ" in detail
