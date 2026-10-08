"""Tests for the local-provider HTTP adapter and gateway fallback wiring."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from clipos_api.llm_gateway import (
    AdapterRegistry,
    LLMDispatchError,
    LLMGateway,
    LLMRequest,
)
from clipos_api.local_adapter import (
    LocalProviderAdapter,
    LocalProviderDispatchError,
    build_local_adapter,
)
from clipos_api.model_registry import HealthState, ModelRegistryStore, ModelRoute

_SUCCESS_PAYLOAD = {
    "choices": [{"message": {"content": "hello from local"}}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
}


class _StubHandler(BaseHTTPRequestHandler):
    mode = "success"

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        assert body["model"] == "route-model-ref"
        assert self.path == "/v1/chat/completions"
        if _StubHandler.mode == "error":
            self.send_response(500)
            self.end_headers()
            return
        if _StubHandler.mode == "slow":
            time.sleep(0.3)
        payload = json.dumps(_SUCCESS_PAYLOAD).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # silence request logging
        pass


@pytest.fixture()
def stub_server() -> str:
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    except PermissionError:
        pytest.skip("socket binding not permitted in this environment")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _request() -> LLMRequest:
    return LLMRequest.model_validate(
        {
            "alias": "writer",
            "messages": [{"role": "user", "content": "hi"}],
            "execution_id": "exec-1",
            "params": {"temperature": 0.2, "max_tokens": 64},
        }
    )


def _route() -> ModelRoute:
    return ModelRoute(
        alias="writer",
        provider="local",
        endpoint_ref="local-endpoint",
        model_ref="route-model-ref",
        priority=0,
    )


def test_adapter_success_response(stub_server: str) -> None:
    adapter = LocalProviderAdapter(stub_server, "route-model-ref")
    response = adapter.send(_request())
    assert response.content == "hello from local"
    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 3
    assert response.model_ref == ""


def test_adapter_connection_refused_raises_dispatch_error() -> None:
    adapter = LocalProviderAdapter("http://127.0.0.1:1", "route-model-ref")
    with pytest.raises(LocalProviderDispatchError, match="failed"):
        adapter.send(_request())


def test_adapter_timeout_raises_dispatch_error(stub_server: str) -> None:
    _StubHandler.mode = "slow"
    adapter = LocalProviderAdapter(stub_server, "route-model-ref", timeout_seconds=0.05)
    with pytest.raises(LocalProviderDispatchError, match="timed out"):
        adapter.send(_request())
    _StubHandler.mode = "success"


def test_adapter_http_error_raises_dispatch_error(stub_server: str) -> None:
    _StubHandler.mode = "error"
    adapter = LocalProviderAdapter(stub_server, "route-model-ref")
    with pytest.raises(LocalProviderDispatchError):
        adapter.send(_request())
    _StubHandler.mode = "success"


def _mock_adapter(stub_server: str) -> LocalProviderAdapter:
    """Same adapter over an in-process transport for socket-restricted runs."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "timeout" in str(request.url):
            raise httpx.ConnectTimeout("timeout", request=request)
        if "error" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, json=_SUCCESS_PAYLOAD)

    transport = httpx.MockTransport(handler)

    def factory(timeout: float) -> httpx.Client:
        return httpx.Client(transport=transport)

    return LocalProviderAdapter(
        stub_server or "http://mock.local", "route-model-ref", client_factory=factory
    )


def test_adapter_success_response_mock_transport() -> None:
    adapter = _mock_adapter("")
    response = adapter.send(_request())
    assert response.content == "hello from local"
    assert response.usage.input_tokens == 7


def test_adapter_timeout_mock_transport() -> None:
    adapter = _mock_adapter("http://timeout.local")
    with pytest.raises(LocalProviderDispatchError, match="timed out"):
        adapter.send(_request())


def test_adapter_http_error_mock_transport() -> None:
    adapter = _mock_adapter("http://error.local")
    with pytest.raises(LocalProviderDispatchError):
        adapter.send(_request())


def test_gateway_falls_back_when_local_fails_mock_transport() -> None:
    registry = _registry_with_two_routes()
    adapters = AdapterRegistry()
    gateway = LLMGateway(registry, adapters)
    failing = _mock_adapter("http://error.local")
    succeeding = _mock_adapter("")
    gateway.register_adapter("local", lambda route: failing)
    gateway.register_adapter("local-mirror", lambda route: succeeding)
    response = gateway.dispatch(_request())
    assert response.content == "hello from local"
    assert response.provider == "local-mirror"


def test_build_local_adapter_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCAL_ADAPTER_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="LOCAL_ADAPTER_BASE_URL"):
        build_local_adapter(_route())
    monkeypatch.setenv("LOCAL_ADAPTER_BASE_URL", "http://127.0.0.1:9999")
    adapter = build_local_adapter(_route())
    assert adapter._model_ref == "route-model-ref"


def test_gateway_falls_back_when_local_fails(stub_server: str) -> None:
    registry = _registry_with_two_routes()
    adapters = AdapterRegistry()
    gateway = LLMGateway(registry, adapters)
    gateway.register_adapter(
        "local", lambda route: LocalProviderAdapter("http://127.0.0.1:1", route.model_ref)
    )
    gateway.register_adapter(
        "local-mirror", lambda route: LocalProviderAdapter(stub_server, route.model_ref)
    )
    response = gateway.dispatch(_request())
    assert response.content == "hello from local"
    assert response.provider == "local-mirror"
    degraded = gateway._registry.list_routes("writer")
    assert degraded[0].health_state is HealthState.DEGRADED
    assert degraded[1].health_state is HealthState.OK


def test_gateway_raises_when_all_routes_fail() -> None:
    registry = _registry_with_two_routes()
    adapters = AdapterRegistry()
    gateway = LLMGateway(registry, adapters)
    broken = LocalProviderAdapter("http://127.0.0.1:1", "ref")
    gateway.register_adapter("local", lambda route: broken)
    gateway.register_adapter("local-mirror", lambda route: broken)
    with pytest.raises(LLMDispatchError, match="all routes failed"):
        gateway.dispatch(_request())


def _registry_with_two_routes() -> ModelRegistryStore:
    from sqlalchemy import create_engine

    from clipos_api.repository import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store = ModelRegistryStore.from_engine(engine)
    store.register(
        ModelRoute(
            alias="writer",
            provider="local",
            endpoint_ref="ep-primary",
            model_ref="route-model-ref",
            priority=0,
        )
    )
    store.register(
        ModelRoute(
            alias="writer",
            provider="local-mirror",
            endpoint_ref="ep-mirror",
            model_ref="route-model-ref",
            priority=1,
        )
    )
    return store


def test_models_ui_page_exists() -> None:
    from pathlib import Path

    page = Path(__file__).resolve().parents[3] / "apps" / "web" / "app" / "models" / "page.tsx"
    text = page.read_text()
    assert "export default async function ModelsPage" in text
    assert "/models" not in text or "API_BASE" in text
    for forbidden in ("llama", "mistral", "qwen", "claude", "gpt-"):
        assert forbidden not in text
