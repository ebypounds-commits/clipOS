"""Local-provider adapter: plain OpenAI-compatible HTTP client for local inference.

The adapter speaks raw HTTP to whatever endpoint is configured through
``LOCAL_ADAPTER_BASE_URL``; it never imports a provider SDK and never embeds a
provider identifier.  Network failures are raised as gateway-dispatch errors so
the fallback chain degrades the route and tries the next one.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import httpx

from clipos_api.llm_gateway import (
    LLMDispatchError,
    LLMRequest,
    LLMResponse,
    ModelRoute,
    ProviderAdapter,
    Usage,
)

_DEFAULT_PROVIDER_NAME = "local"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_ENV_BASE_URL = "LOCAL_ADAPTER_BASE_URL"
_ENV_PROVIDER = "LOCAL_ADAPTER_PROVIDER"
_ENV_TIMEOUT = "LOCAL_ADAPTER_TIMEOUT_SECONDS"


class LocalProviderDispatchError(LLMDispatchError):
    """Raised when the local inference endpoint is unreachable or misbehaves."""


class LocalProviderAdapter(ProviderAdapter):
    """OpenAI-compatible chat-completions client for a local inference server."""

    def __init__(
        self,
        base_url: str,
        model_ref: str,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client_factory: Callable[..., httpx.Client] | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url must be non-empty")
        if not model_ref:
            raise ValueError("model_ref must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._base_url = base_url.rstrip("/")
        self._model_ref = model_ref
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory or httpx.Client

    def send(self, request: LLMRequest) -> LLMResponse:
        """POST one chat completion request; network errors are dispatch errors."""
        payload: dict[str, Any] = {
            "model": self._model_ref,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if request.params.temperature is not None:
            payload["temperature"] = request.params.temperature
        if request.params.max_tokens is not None:
            payload["max_tokens"] = request.params.max_tokens
        try:
            with self._client_factory(timeout=self._timeout_seconds) as client:
                response = client.post(f"{self._base_url}/v1/chat/completions", json=payload)
                response.raise_for_status()
                data: dict[str, Any] = response.json()
        except httpx.TimeoutException as exc:
            raise LocalProviderDispatchError(
                f"local provider timed out after {self._timeout_seconds}s"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LocalProviderDispatchError(f"local provider request failed: {exc}") from exc
        return self._response_from_payload(data)

    def _response_from_payload(self, data: dict[str, Any]) -> LLMResponse:
        choices = data.get("choices")
        content = ""
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else {}
            if isinstance(message, dict):
                content = str(message.get("content") or "")
        raw_usage = data.get("usage")
        usage_data: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        return LLMResponse(
            content=content,
            usage=Usage(
                input_tokens=int(usage_data.get("prompt_tokens") or 0),
                output_tokens=int(usage_data.get("completion_tokens") or 0),
            ),
        )


def build_local_adapter(route: ModelRoute) -> LocalProviderAdapter:
    """Factory wired by production startup; reads configuration from env only."""
    base_url = os.environ.get(_ENV_BASE_URL, "")
    if not base_url:
        raise ValueError("LOCAL_ADAPTER_BASE_URL is not configured")
    timeout_raw = os.environ.get(_ENV_TIMEOUT)
    timeout = float(timeout_raw) if timeout_raw else _DEFAULT_TIMEOUT_SECONDS
    return LocalProviderAdapter(base_url, route.model_ref, timeout_seconds=timeout)


def local_adapter_provider_name() -> str:
    """Registry provider name under which the local adapter is registered."""
    return os.environ.get(_ENV_PROVIDER) or _DEFAULT_PROVIDER_NAME
