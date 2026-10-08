"""Tests for the SDK-free MCP server boundary (CLIP-028)."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit, main, mcp_boundary, plugin_registry, tool_registry
from clipos_api.mcp_boundary import (
    McpBoundary,
    McpBoundaryError,
    McpToolCall,
    default_endpoint_provider,
)
from clipos_api.plugin_registry import (
    PluginDef,
    PluginRegistryStore,
    PluginType,
)
from clipos_api.tool_registry import ToolRegistryStore


@dataclass(frozen=True)
class StubServer:
    endpoint: str
    client_factory: Callable[..., httpx.Client] | None = None


class StubMCPHandler(BaseHTTPRequestHandler):
    """Thread-backed JSON-RPC endpoint with request capture."""

    requests: ClassVar[list[dict[str, Any]]] = []
    response_status: ClassVar[int] = 200
    response_body: ClassVar[dict[str, Any]] = {}

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).requests.append(body)
        encoded = json.dumps(type(self).response_body).encode("utf-8")
        self.send_response(type(self).response_status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.fixture()
def engine() -> Engine:
    created = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tool_registry.Base.metadata.create_all(created)
    plugin_registry.Base.metadata.create_all(created)
    return created


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-028-mcp-boundary-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def plugins(engine: Engine, audit_log: Path) -> PluginRegistryStore:
    return PluginRegistryStore.from_engine(engine)


@pytest.fixture()
def mock_server():
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            StubMCPHandler.response_status,
            json=StubMCPHandler.response_body,
        )

    return StubServer(
        "http://mcp.test/mcp",
        lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler), **kwargs),
    ), captured


@pytest.fixture()
def server():
    handler = StubMCPHandler
    handler.requests = []
    handler.response_status = 200
    handler.response_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "tool output"}]},
    }
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except PermissionError as exc:
        pytest.skip(f"localhost sockets unavailable: {exc}")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield StubServer(f"http://127.0.0.1:{httpd.server_port}/mcp")
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


@pytest.fixture()
def endpoint(monkeypatch: pytest.MonkeyPatch, server: StubServer) -> str:
    monkeypatch.setenv("MCP_TEST_ENDPOINT", server.endpoint)
    return "MCP_TEST_ENDPOINT"


def _plugin(
    plugins_store: PluginRegistryStore,
    plugin_id: str = "internal-mcp",
    **changes: object,
) -> PluginDef:
    values: dict[str, Any] = {
        "plugin_id": plugin_id,
        "plugin_type": PluginType.MCP_SERVER,
        "name": "Internal MCP",
        "config_ref": "MCP_TEST_ENDPOINT",
        "metadata": {"hosting": "self_hosted", "timeout_seconds": 5},
    }
    values.update(changes)
    plugin = PluginDef.model_validate(values)
    plugins_store.register(plugin)
    plugins_store.set_enabled(plugin.plugin_id, True)
    return plugin


def _boundary(
    plugins_store: PluginRegistryStore,
    *,
    capable: bool = True,
    client_factory: Callable[..., httpx.Client] | None = None,
) -> McpBoundary:
    return McpBoundary(
        plugins_store,
        default_endpoint_provider,
        lambda agent_id, version, tool_id: capable,
        client_factory=client_factory,
    )


def _call(plugin_id: str = "internal-mcp", execution_id: str = "execution-1") -> McpToolCall:
    return McpToolCall(
        plugin_id=plugin_id,
        tool_name="search_notes",
        arguments={"query": "secret-argument-value"},
        execution_id=execution_id,
    )


def test_successful_round_trip_is_json_rpc_capability_gated_and_idempotent(
    plugins: PluginRegistryStore, server: StubServer, audit_log: Path, endpoint: str
) -> None:
    plugins.register(_plugin(plugins))
    boundary = _boundary(plugins, client_factory=server.client_factory)
    request = _call()

    first = boundary.call(request, agent_id="operations", agent_version="v1")
    second = boundary.call(request, agent_id="operations", agent_version="v1")

    assert first.ok is True
    assert first.error_class is None
    assert first.content == [{"type": "text", "text": "tool output"}]
    stub_endpoint = server.endpoint
    assert first.latency_ms >= 0
    assert second == first
    assert len(StubMCPHandler.requests) == 1
    assert stub_endpoint.endswith("/mcp")
    payload = StubMCPHandler.requests[0]
    assert payload == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search_notes", "arguments": {"query": "secret-argument-value"}},
    }
    events = audit_log.read_text(encoding="utf-8")
    assert events.count("mcp_boundary.tool_called") == 1
    assert "search_notes" in events
    assert "internal-mcp" in events
    assert "arguments" not in events
    assert "secret-argument-value" not in events


def test_json_rpc_round_trip_with_injected_transport(
    plugins: PluginRegistryStore,
    mock_server: tuple[StubServer, list[dict[str, Any]]],
    endpoint: str,
    audit_log: Path,
) -> None:
    """Cover the offline path used when this runner cannot bind localhost."""
    stub, captured = mock_server
    plugins.register(_plugin(plugins))
    result = _boundary(plugins, client_factory=stub.client_factory).call(
        _call(), agent_id="operations", agent_version="v1"
    )

    assert result.ok is True
    assert result.content == [{"type": "text", "text": "tool output"}]
    assert captured == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_notes", "arguments": {"query": "secret-argument-value"}},
        }
    ]
    assert "secret-argument-value" not in audit_log.read_text(encoding="utf-8")


def test_wrong_type_disabled_and_capability_plugins_are_denied(
    plugins: PluginRegistryStore, audit_log: Path
) -> None:
    plugins.register(_plugin(plugins, "wrong-type", plugin_type=PluginType.NATIVE_ADAPTER))
    plugins.register(_plugin(plugins, "disabled-mcp"))
    plugins.set_enabled("disabled-mcp", False)
    boundary = _boundary(plugins)

    for plugin_id, reason in [
        ("wrong-type", "wrong_plugin_type"),
        ("disabled-mcp", "plugin_disabled"),
    ]:
        with pytest.raises(McpBoundaryError, match=reason):
            boundary.call(_call(plugin_id), agent_id="operations", agent_version="v1")

    plugins.register(_plugin(plugins))
    with pytest.raises(McpBoundaryError, match="capability_denied"):
        _boundary(plugins, capable=False).call(_call(), agent_id="operations", agent_version="v1")

    events = audit_log.read_text(encoding="utf-8")
    assert events.count("mcp_boundary.call_denied") == 3
    assert "arguments" not in events
    assert "secret-argument-value" not in events


def test_hosted_server_requires_explicit_trust(
    plugins: PluginRegistryStore,
    mock_server: tuple[StubServer, list[dict[str, Any]]],
    endpoint: str,
    audit_log: Path,
) -> None:
    stub, _ = mock_server
    plugins.register(_plugin(plugins, "hosted-untrusted", metadata={"hosting": "hosted"}))
    plugins.register(
        _plugin(
            plugins,
            "hosted-trusted",
            metadata={"hosting": "hosted", "trusted": True},
        )
    )
    boundary = _boundary(plugins, client_factory=stub.client_factory)

    with pytest.raises(McpBoundaryError, match="hosted_server_not_trusted"):
        boundary.call(
            _call("hosted-untrusted", "execution-untrusted"),
            agent_id="operations",
            agent_version="v1",
        )
    result = boundary.call(
        _call("hosted-trusted", "execution-trusted"),
        agent_id="operations",
        agent_version="v1",
    )

    assert result.ok is True
    events = audit_log.read_text(encoding="utf-8")
    assert events.count("mcp_boundary.call_denied") == 1
    assert events.count("mcp_boundary.tool_called") == 1


def test_network_refusal_is_failure_safe(plugins: PluginRegistryStore, audit_log: Path) -> None:
    plugins.register(_plugin(plugins, "unreachable-mcp"))
    transport = httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request))
    )
    boundary = McpBoundary(
        plugins,
        lambda config_ref: "http://127.0.0.1:1/mcp",
        lambda agent_id, version, tool_id: True,
        timeout_seconds=1,
        client_factory=lambda **kwargs: httpx.Client(transport=transport, **kwargs),
    )

    result = boundary.call(_call("unreachable-mcp"), agent_id="ops", agent_version="v1")

    assert result.ok is False
    assert result.error_class == "network_error"
    assert result.content is None
    assert "mcp_boundary.call_failed" in audit_log.read_text(encoding="utf-8")


def test_rpc_error_and_bad_response_do_not_cache_failures(
    plugins: PluginRegistryStore,
    mock_server: tuple[StubServer, list[dict[str, Any]]],
    endpoint: str,
    audit_log: Path,
) -> None:
    stub, captured = mock_server
    plugins.register(_plugin(plugins, "rpc-error"))
    StubMCPHandler.response_body = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601}}
    boundary = _boundary(plugins, client_factory=stub.client_factory)
    first = boundary.call(_call("rpc-error", "rpc-exec"), agent_id="operations", agent_version="v1")
    assert first.ok is False
    assert first.error_class == "rpc_error"

    StubMCPHandler.response_body = {"unexpected": True}
    second = boundary.call(
        _call("rpc-error", "rpc-exec"), agent_id="operations", agent_version="v1"
    )
    assert second.ok is False
    assert second.error_class == "invalid_response"
    assert len(captured) == 2
    events = audit_log.read_text(encoding="utf-8")
    assert events.count("mcp_boundary.call_failed") == 2
    assert events.count("mcp_boundary.tool_called") == 0


def test_endpoint_provider_resolves_reference_without_inline_urls() -> None:
    with pytest.raises(McpBoundaryError, match="valid endpoint environment"):
        default_endpoint_provider("https://inline.example/mcp")
    with pytest.raises(McpBoundaryError, match="not configured"):
        default_endpoint_provider("__MISSING_ENDPOINT_TEST__")


def test_production_factory_is_wired(engine: Engine) -> None:
    boundary = main.build_mcp_client_boundary(engine)
    assert isinstance(boundary, McpBoundary)
    assert isinstance(boundary._plugins, PluginRegistryStore)
    assert isinstance(boundary._capabilities.__self__, ToolRegistryStore)


def test_no_mcp_sdk_is_imported_and_no_provider_model_ids_hard_coded() -> None:
    code = "import sys, clipos_api.mcp_boundary; print('mcp' in sys.modules)"
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == "False"
    source = Path(mcp_boundary.__file__).read_text(encoding="utf-8").lower()
    for marker in ("gpt-", "claude-", "gemini-", "llama-", "mistral-"):
        assert marker not in source
