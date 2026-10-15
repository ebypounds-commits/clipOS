"""Provider adapters and HTTP dispatch endpoint (Plan: provider wiring).

Wires operators' LLM providers behind the existing gateway contracts.
Every provider identity — name, endpoint, wire protocol, credential
env — lives in the *database* route config (provider / endpoint_ref /
metadata) written by ``scripts/setup_llm_providers.py``; this module
holds only protocol implementations and deliberately neutral tokens:

- **compat protocol** — ``{endpoint}/chat/completions`` with
  ``Authorization: Bearer``: any OpenAI-wire endpoint (public SaaS or
  self-hosted).
- **messages protocol** — ``{endpoint}/messages`` with ``x-api-key`` +
  version header, system prompts hoisted out of the message list:
  Anthropic-style endpoints.
- **gen-content protocol** — ``{endpoint}/models/{model}:generateContent``
  with ``x-goog-api-key``: Google-style endpoints.

Route metadata contract (written at registration time, never compiled
in): ``protocol`` (compat | messages | gen-content), ``api_key_env``
(env var holding the credential), optional ``timeout_seconds``.
Credentials resolve lazily at send time; a missing key raises
``ProviderDispatchError`` so the gateway degrades that route and falls
back to the next alias entry — never aborts the chain. Endpoint
resolution: ``route.endpoint_ref`` when it is a URL, otherwise the
provider-derived ``{PROVIDER}_BASE_URL`` env (values computed at
runtime from the route's own provider string).

**HTTP dispatch** — ``POST /models/{alias}/dispatch`` exposes
``LLMGateway.dispatch`` over HTTP (messages + execution_id +
optional params): 404 unknown alias, 502 when every route in the
fallback chain failed (all keys missing / all upstream errors).
Successful completions are recorded by the cost ledger and visible at
GET /models/{alias}/dispatches.

``main.build_llm_gateway`` registers ``build_provider_adapter`` for
every distinct provider present in the Model Registry — registration
is data-driven, no provider names compiled into source. No SDK
imports — plain httpx, mirroring the local adapter.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from clipos_api.llm_gateway import (
    ChatMessage,
    LLMDispatchError,
    LLMGateway,
    LLMRequest,
    LLMResponse,
    ProviderAdapter,
    RequestParams,
    Usage,
)

_DEFAULT_TIMEOUT_SECONDS = 60.0
_ANTHROPIC_VERSION = "2023-06-01"
_ClientFactory = Callable[..., httpx.Client]

# Route metadata contract (written by scripts/setup_llm_providers.py):
#   protocol      compat | messages | gen-content
#   api_key_env   environment variable holding the credential
#   timeout_seconds (optional)
# Endpoints come from route.endpoint_ref (URL) or {PROVIDER}_BASE_URL env.


class ProviderDispatchError(LLMDispatchError):
    """Route-level provider failure (key missing, wire error)."""


def _resolve_endpoint(provider: str, endpoint_ref: str) -> str:
    """Endpoint from route ref when it is a URL, else provider env."""
    if endpoint_ref.startswith(("http://", "https://")):
        return endpoint_ref.rstrip("/")
    env_name = f"{provider.upper()}_BASE_URL"
    endpoint = os.environ.get(env_name, "")
    if not endpoint:
        raise ProviderDispatchError(
            f"no endpoint for provider {provider}: set route endpoint_ref or {env_name}"
        )
    return endpoint.rstrip("/")


def _resolve_key_env(metadata: dict[str, Any]) -> str:
    key_env = metadata.get("api_key_env")
    if not isinstance(key_env, str) or not key_env:
        raise ProviderDispatchError(
            "route metadata must include api_key_env (see setup_llm_providers)"
        )
    return key_env


class _BaseAdapter(ProviderAdapter):
    """Shared httpx/wire plumbing for the three provider protocols."""

    def __init__(
        self,
        endpoint: str,
        api_key_env: str,
        model_ref: str,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client_factory: _ClientFactory | None = None,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint must be non-empty")
        if not model_ref:
            raise ValueError("model_ref must be non-empty")
        self._endpoint = endpoint.rstrip("/")
        self._api_key_env = api_key_env
        self._model_ref = model_ref
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory or httpx.Client

    def _api_key(self) -> str:
        key = os.environ.get(self._api_key_env, "")
        if not key:
            raise ProviderDispatchError(f"provider api key missing: set {self._api_key_env}")
        return key

    @staticmethod
    def _usage(input_tokens: int, output_tokens: int) -> Usage:
        return Usage(
            input_tokens=max(0, int(input_tokens)),
            output_tokens=max(0, int(output_tokens)),
        )


class OpenAICompatAdapter(_BaseAdapter):
    """OpenAI-wire chat completions (compat protocol: bearer auth)."""

    def send(self, request: LLMRequest) -> LLMResponse:
        key = self._api_key()
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
                response = client.post(
                    f"{self._endpoint}/chat/completions",
                    json=payload,
                    headers={"authorization": f"Bearer {key}"},
                )
                response.raise_for_status()
                data: dict[str, Any] = response.json()
        except httpx.TimeoutException as exc:
            raise ProviderDispatchError(
                f"provider timed out after {self._timeout_seconds}s"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderDispatchError(f"provider request failed: {exc}") from exc
        content = ""
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                content = str(message.get("content") or "")
        raw_usage = data.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        return LLMResponse(
            content=content,
            usage=self._usage(
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
            ),
        )


class AnthropicAdapter(_BaseAdapter):
    """Anthropic Messages wire (claude): system hoisted, versioned headers."""

    def send(self, request: LLMRequest) -> LLMResponse:
        key = self._api_key()
        system_parts = [m.content for m in request.messages if m.role == "system"]
        turns = [
            {"role": m.role, "content": m.content}
            for m in request.messages
            if m.role in ("user", "assistant")
        ]
        if not turns:
            raise ProviderDispatchError("anthropic request needs user/assistant turns")
        payload: dict[str, Any] = {
            "model": self._model_ref,
            "max_tokens": request.params.max_tokens or 1024,
            "messages": turns,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.params.temperature is not None:
            payload["temperature"] = request.params.temperature
        headers = {
            "x-api-key": key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        try:
            with self._client_factory(timeout=self._timeout_seconds) as client:
                response = client.post(
                    f"{self._endpoint}/messages",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data: dict[str, Any] = response.json()
        except httpx.TimeoutException as exc:
            raise ProviderDispatchError(
                f"provider timed out after {self._timeout_seconds}s"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderDispatchError(f"provider request failed: {exc}") from exc
        content = ""
        blocks = data.get("content")
        if isinstance(blocks, list):
            content = "".join(
                str(b.get("text") or "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
        raw_usage = data.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        return LLMResponse(
            content=content,
            usage=self._usage(
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
            ),
        )


class GeminiAdapter(_BaseAdapter):
    """generateContent wire (gen-content) with x-goog-api-key auth."""

    def send(self, request: LLMRequest) -> LLMResponse:
        key = self._api_key()
        system_parts = [m.content for m in request.messages if m.role == "system"]
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in request.messages
            if m.role in ("user", "assistant")
        ]
        if not contents:
            raise ProviderDispatchError("gen-content request needs user/assistant turns")
        payload: dict[str, Any] = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        generation: dict[str, Any] = {}
        if request.params.temperature is not None:
            generation["temperature"] = request.params.temperature
        if request.params.max_tokens is not None:
            generation["maxOutputTokens"] = request.params.max_tokens
        if generation:
            payload["generationConfig"] = generation
        try:
            with self._client_factory(timeout=self._timeout_seconds) as client:
                response = client.post(
                    f"{self._endpoint}/models/{self._model_ref}:generateContent",
                    json=payload,
                    headers={"x-goog-api-key": key},
                )
                response.raise_for_status()
                data: dict[str, Any] = response.json()
        except httpx.TimeoutException as exc:
            raise ProviderDispatchError(
                f"provider timed out after {self._timeout_seconds}s"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderDispatchError(f"provider request failed: {exc}") from exc
        content = ""
        candidates = data.get("candidates")
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
            candidate_content = candidates[0].get("content")
            if isinstance(candidate_content, dict):
                parts = candidate_content.get("parts")
                if isinstance(parts, list):
                    content = "".join(
                        str(p.get("text") or "") for p in parts if isinstance(p, dict)
                    )
        raw_usage = data.get("usageMetadata")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        return LLMResponse(
            content=content,
            usage=self._usage(
                int(usage.get("promptTokenCount") or 0),
                int(usage.get("candidatesTokenCount") or 0),
            ),
        )


def build_provider_adapter(route) -> ProviderAdapter:
    """Factory: protocol + endpoint + credentials all from route config.

    metadata.protocol selects the wire protocol: `messages` (Anthropic
    style), `gen-content` (Google style), default `compat`
    (OpenAI-wire chat completions).
    """
    metadata = dict(route.metadata or {})
    endpoint = _resolve_endpoint(route.provider, route.endpoint_ref or "")
    key_env = _resolve_key_env(metadata)
    timeout = float(metadata.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)
    protocol = str(metadata.get("protocol") or "compat")
    if protocol == "messages":
        adapter_cls: type[_BaseAdapter] = AnthropicAdapter
    elif protocol in ("gen-content", "gencontent"):
        adapter_cls = GeminiAdapter
    else:
        adapter_cls = OpenAICompatAdapter
    return adapter_cls(endpoint, key_env, route.model_ref, timeout_seconds=timeout)


# ---------------------------------------------------------------------------
# HTTP dispatch endpoint
# ---------------------------------------------------------------------------


class DispatchMessage(BaseModel):
    """One message in an HTTP dispatch request."""

    model_config = ConfigDict(frozen=True)

    role: str = Field(min_length=1, max_length=64)
    content: str


class DispatchParams(BaseModel):
    """Optional generation params."""

    model_config = ConfigDict(frozen=True)

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)


class DispatchRequest(BaseModel):
    """POST /models/{alias}/dispatch body."""

    model_config = ConfigDict(frozen=True)

    execution_id: str = Field(min_length=1, max_length=128)
    messages: list[DispatchMessage] = Field(min_length=1, max_length=64)
    params: DispatchParams = Field(default_factory=DispatchParams)


def _gateway(engine) -> LLMGateway:
    """Build a fresh gateway bound to this process database (test seam).

    build_llm_gateway already registers the local adapter plus every
    provider discovered from the Model Registry.
    """
    from clipos_api.main import build_llm_gateway

    return build_llm_gateway(engine)


def _missing_key_envs(routes) -> list[str]:
    """Env names (never values) for chain routes whose credential is unset."""
    missing: set[str] = set()
    for route in routes:
        env_name = dict(route.metadata or {}).get("api_key_env")
        if isinstance(env_name, str) and env_name and not os.environ.get(env_name):
            missing.add(env_name)
    return sorted(missing)


def dispatch_alias(
    alias: str,
    request: DispatchRequest,
    engine,
) -> LLMResponse:
    """Dispatch one execution over the alias fallback chain."""
    from clipos_api.model_registry import ModelRegistryStore

    registry = ModelRegistryStore.from_engine(engine)
    try:
        routes = registry.resolve_alias(alias)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not routes:
        raise HTTPException(status_code=404, detail=f"no enabled routes for alias: {alias}")
    gateway = _gateway(engine)
    llm_request = LLMRequest(
        alias=alias,
        messages=[ChatMessage(role=m.role, content=m.content) for m in request.messages],
        params=RequestParams(
            temperature=request.params.temperature,
            max_tokens=request.params.max_tokens,
        ),
        execution_id=request.execution_id,
    )
    try:
        return gateway.dispatch(llm_request)
    except LLMDispatchError as exc:
        missing = _missing_key_envs(routes)
        if missing:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"dispatch failed for alias {alias}: all routes failed — "
                    f"missing api key env: {', '.join(missing)}"
                ),
            ) from exc
        raise HTTPException(
            status_code=502, detail=f"dispatch failed for alias {alias}: {exc}"
        ) from exc


router = APIRouter(tags=["llm-providers"])


def _engine():
    from clipos_api.main import database_engine

    return database_engine()


@router.post(
    "/models/{alias}/dispatch",
    response_model=LLMResponse,
    name="dispatch_model_alias",
)
def dispatch_model_alias_route(
    alias: str,
    request: DispatchRequest,
    engine: Annotated[Any, Depends(_engine)],
) -> LLMResponse:
    """Dispatch one completion over the alias fallback chain (502 when all fail)."""
    return dispatch_alias(alias, request, engine)
