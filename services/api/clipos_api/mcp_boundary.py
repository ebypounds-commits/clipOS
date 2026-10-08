"""Typed, audited client boundary for MCP servers over JSON-RPC 2.0 HTTP.

This module deliberately speaks a minimal ``tools/call`` subset of MCP over
HTTP.  It does not import an MCP SDK: the call contract is JSON-RPC, plugin
access is governed by the plugin registry, tool invocation is capability
gated, and endpoints are resolved only through an operator-supplied provider.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.plugin_registry import PluginRegistryStore, PluginState, PluginType
from clipos_api.tool_registry import ToolRegistryStore

_DEFAULT_ACTOR = "mcp-boundary"
_DEFAULT_TIMEOUT_SECONDS = 30.0

EndpointProvider = Callable[[str], str]
CapabilityChecker = Callable[[str, str, str], bool]
_ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class McpResponseError(ValueError):
    """Raised when an MCP response violates the minimal JSON-RPC contract."""


class McpBoundaryError(RuntimeError):
    """Raised when policy prevents an MCP call; never for transport failures."""


class McpToolCall(BaseModel):
    """A capability-authorized request addressed to one registered MCP plugin."""

    model_config = ConfigDict(frozen=True)

    plugin_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    execution_id: str = Field(min_length=1)


class McpToolResult(BaseModel):
    """Failure-safe result of one MCP tools/call attempt."""

    ok: bool
    content: Any = None
    error_class: str | None = None
    latency_ms: int = 0


def default_endpoint_provider(config_ref: str) -> str:
    """Resolve a non-inline MCP endpoint reference from an environment variable.

    The config reference is intentionally not treated as a URL.  For an MCP
    server it names the endpoint-bearing environment variable, for example
    ``MCP_INTERNAL_ENDPOINT``.  This keeps network destinations in operator
    configuration and prevents endpoint or credential material from entering
    the plugin registry.
    """
    if not _ENV_NAME_PATTERN.fullmatch(config_ref):
        raise McpBoundaryError("MCP config_ref must name a valid endpoint environment variable")
    endpoint = os.environ.get(config_ref, "")
    if not endpoint:
        raise McpBoundaryError(f"MCP endpoint is not configured: {config_ref}")
    return endpoint


class McpBoundary:
    """Trust and capability-aware JSON-RPC client for registered MCP servers."""

    def __init__(
        self,
        plugins: PluginRegistryStore,
        endpoint_provider: EndpointProvider,
        capabilities: CapabilityChecker,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client_factory: Callable[..., httpx.Client] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._plugins = plugins
        self._endpoint_provider = endpoint_provider
        self._capabilities = capabilities
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory or httpx.Client
        self._cache: dict[str, McpToolResult] = {}

    @staticmethod
    def _audit(action: str, subject: str, details: dict[str, Any]) -> None:
        audit.record(action=action, actor=_DEFAULT_ACTOR, subject=subject, details=details)

    @staticmethod
    def _deny(plugin_id: str, request: McpToolCall, reason: str) -> McpBoundaryError:
        McpBoundary._audit(
            "mcp_boundary.call_denied",
            f"plugin:{plugin_id}",
            {
                "plugin_id": plugin_id,
                "tool_name": request.tool_name,
                "execution_id": request.execution_id,
                "reason": reason,
            },
        )
        return McpBoundaryError(f"MCP call denied: {reason}")

    @staticmethod
    def _trusted_host(plugin: PluginState) -> bool:
        metadata = plugin.plugin.metadata
        hosting = metadata.get("hosting")
        hosted = metadata.get("hosted") is True or hosting == "hosted"
        if hosted:
            return metadata.get("trusted") is True
        # Private/self-hosted servers are trusted by default.  Unknown hosting
        # labels are treated as hosted so the safer path requires trust.
        return hosting in {"private", "self_hosted", "self-hosted"}

    def _plugin(self, request: McpToolCall) -> PluginState:
        plugin = self._plugins.get_plugin(request.plugin_id)
        if plugin is None:
            raise self._deny(request.plugin_id, request, "plugin_not_found")
        if plugin.plugin.plugin_type is not PluginType.MCP_SERVER:
            raise self._deny(request.plugin_id, request, "wrong_plugin_type")
        if not plugin.enabled:
            raise self._deny(request.plugin_id, request, "plugin_disabled")
        if not self._trusted_host(plugin):
            raise self._deny(request.plugin_id, request, "hosted_server_not_trusted")
        return plugin

    def call(
        self,
        request: McpToolCall,
        *,
        agent_id: str,
        agent_version: str,
    ) -> McpToolResult:
        """Call one MCP tool idempotently without exposing request arguments.

        Policy denials raise ``McpBoundaryError`` after an audited denial.
        Transport, timeout, HTTP, and malformed-response failures become
        failure-safe results.  A successful result is cached for the execution
        identity so replaying the same request does not repeat the side effect.
        """
        cached = self._cache.get(request.execution_id)
        if cached is not None:
            return cached
        plugin = self._plugin(request)
        if not self._capabilities(agent_id, agent_version, request.tool_name):
            raise self._deny(request.plugin_id, request, "capability_denied")

        started = time.monotonic()
        try:
            endpoint = self._endpoint_provider(plugin.plugin.config_ref)
            if not endpoint:
                raise ValueError("endpoint is empty")
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": request.tool_name,
                    "arguments": request.arguments,
                },
            }
            with self._client_factory(timeout=self._timeout_seconds) as client:
                response = client.post(endpoint, json=payload)
                response.raise_for_status()
                body: Any = response.json()
            if not isinstance(body, dict):
                raise McpResponseError("response is not a JSON object")
            if isinstance(body.get("error"), dict):
                error_class = "rpc_error"
                result = McpToolResult(ok=False, error_class=error_class)
            elif not isinstance(body.get("result"), dict):
                raise McpResponseError("JSON-RPC result is not an object")
            else:
                rpc_result: dict[str, Any] = body["result"]
                result = McpToolResult(
                    ok=True,
                    content=rpc_result.get("content", rpc_result),
                    latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                )
        except httpx.TimeoutException:
            result = self._failure(started, "timeout")
        except httpx.HTTPStatusError:
            result = self._failure(started, "http_error")
        except httpx.RequestError:
            result = self._failure(started, "network_error")
        except httpx.HTTPError:
            result = self._failure(started, "http_error")
        except ValueError:
            result = self._failure(started, "invalid_response")
        except Exception as exc:  # noqa: BLE001 - the boundary is failure-safe
            result = self._failure(started, type(exc).__name__)
        if result.ok:
            self._cache[request.execution_id] = result
            self._audit(
                "mcp_boundary.tool_called",
                f"plugin:{request.plugin_id}",
                {
                    "plugin_id": request.plugin_id,
                    "tool_name": request.tool_name,
                    "execution_id": request.execution_id,
                    "latency_ms": result.latency_ms,
                },
            )
        else:
            self._audit(
                "mcp_boundary.call_failed",
                f"plugin:{request.plugin_id}",
                {
                    "plugin_id": request.plugin_id,
                    "tool_name": request.tool_name,
                    "execution_id": request.execution_id,
                    "error_class": result.error_class,
                    "latency_ms": result.latency_ms,
                },
            )
        return result

    @staticmethod
    def _failure(started: float, error_class: str) -> McpToolResult:
        return McpToolResult(
            ok=False,
            error_class=error_class,
            latency_ms=max(0, round((time.monotonic() - started) * 1000)),
        )


def build_mcp_boundary(engine: Engine) -> McpBoundary:
    """Production wiring point for the registry-backed MCP boundary."""
    return McpBoundary(
        PluginRegistryStore.from_engine(engine),
        default_endpoint_provider,
        ToolRegistryStore.from_engine(engine).check_capability,
    )
