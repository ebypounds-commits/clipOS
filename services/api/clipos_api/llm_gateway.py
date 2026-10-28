"""LLM Gateway: logical alias dispatch over registered provider adapters.

The gateway resolves a logical model alias through the Model Registry fallback
chain and dispatches to provider adapters in priority order.  Provider SDKs are
never imported at module scope: adapters are registered by name at startup via
adapter factories (lazy, injected).  Dispatches are failure-safe (audited start
and finish), degrade unhealthy routes audited, and are idempotent per
``execution_id`` via an in-memory response cache.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from clipos_api import audit
from clipos_api.model_registry import HealthState, ModelRegistryStore, ModelRoute

if TYPE_CHECKING:
    from clipos_api.llm_cost import CostLedger, DispatchRecord

_GATEWAY_ACTOR = "llm-gateway"


class ChatMessage(BaseModel):
    """One untrusted message in a provider-neutral conversation."""

    role: str = Field(min_length=1, max_length=64)
    content: str


class RequestParams(BaseModel):
    """Optional generation parameters forwarded to the provider adapter."""

    model_config = ConfigDict(frozen=True)

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)


class LLMRequest(BaseModel):
    """Provider-neutral request addressed to a logical model alias."""

    model_config = ConfigDict(frozen=True)

    alias: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    params: RequestParams = Field(default_factory=RequestParams)
    execution_id: str = Field(min_length=1)


class Usage(BaseModel):
    """Token usage reported for one completion."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class LLMResponse(BaseModel):
    """Provider-neutral completion; gateway-owned fields are set by dispatch."""

    alias: str = ""
    provider: str = ""
    model_ref: str = ""
    content: str = ""
    usage: Usage = Field(default_factory=Usage)
    latency_ms: int = 0
    execution_id: str = ""
    adapter_kind: Literal["mock", "real"] = "real"


class ProviderAdapter(ABC):
    """Adapter contract implemented by production or test provider clients."""

    @abstractmethod
    def send(self, request: LLMRequest) -> LLMResponse:
        """Send one request to the bound provider and return a completion."""


AdapterFactory = Callable[[ModelRoute], ProviderAdapter]


class AdapterRegistry:
    """Pluggable, name-keyed adapter factories; no provider SDK imports here."""

    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register_adapter(self, provider_name: str, factory: AdapterFactory) -> None:
        """Register (or replace) the factory for one provider route name."""
        if not provider_name:
            raise ValueError("provider_name must be non-empty")
        self._factories[provider_name] = factory

    def adapter_for(self, route: ModelRoute) -> ProviderAdapter:
        """Lazily construct the adapter for one route."""
        factory = self._factories.get(route.provider)
        if factory is None:
            raise ValueError(f"no adapter registered for provider: {route.provider}")
        return factory(route)


class LLMDispatchError(RuntimeError):
    """Raised when every route in the alias fallback chain failed."""


_DEFAULT_DEGRADED_COOLDOWN_SECONDS = 300.0


class LLMGateway:
    """Alias-resolving dispatch client over an ordered model-route registry."""

    def __init__(
        self,
        registry: ModelRegistryStore,
        adapters: AdapterRegistry,
        *,
        ledger: CostLedger | None = None,
        degraded_cooldown_seconds: float = _DEFAULT_DEGRADED_COOLDOWN_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._adapters = adapters
        self._ledger = ledger
        self._cooldown_seconds = max(0.0, degraded_cooldown_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache: dict[str, LLMResponse] = {}

    def register_adapter(self, provider_name: str, factory: AdapterFactory) -> None:
        """Register one provider adapter factory (tests/production wiring)."""
        self._adapters.register_adapter(provider_name, factory)

    def _audit(self, action: str, subject: str, details: dict[str, Any]) -> None:
        audit.record(action=action, actor=_GATEWAY_ACTOR, subject=subject, details=details)

    def _record(self, record: DispatchRecord) -> None:
        """Persist one dispatch attempt; ledger failures never mask dispatch ones."""
        if self._ledger is None:
            return
        try:
            self._ledger.record(record)
        except Exception:  # noqa: BLE001 - persistence must not mask dispatch outcomes
            self._audit(
                "llm_gateway.dispatch_record_failed",
                f"execution:{record.execution_id}",
                {"provider": record.provider, "model_ref": record.model_ref},
            )

    def _eligible_routes(self, alias: str) -> list[ModelRoute]:
        """Resolve enabled routes, re-admitting degraded ones after cooldown."""
        now = self._clock()
        eligible: list[ModelRoute] = []
        for route in self._registry.list_routes(alias):
            if not route.enabled or route.health_state is HealthState.DISABLED:
                continue
            if route.health_state is HealthState.DEGRADED:
                degraded_at = route.updated_at
                if degraded_at is None:
                    continue
                elapsed = (now - degraded_at).total_seconds()
                if elapsed < self._cooldown_seconds:
                    continue
            eligible.append(route)
        return eligible

    def _degrade_route(self, route: ModelRoute) -> None:
        """Mark a failed route degraded; audit failures never mask dispatch ones."""
        try:
            self._registry.set_route_health(
                route.alias, route.provider, route.model_ref, HealthState.DEGRADED
            )
        except Exception:  # noqa: BLE001 - audit/persistence must not mask dispatch failure
            self._audit(
                "llm_gateway.health_update_failed",
                f"model_route:{route.alias}:{route.provider}:{route.model_ref}",
                {},
            )

    def _recover_route(self, route: ModelRoute) -> None:
        """Mark a cooldown-recovered route healthy again; audit failures never mask."""
        try:
            self._registry.set_route_health(
                route.alias, route.provider, route.model_ref, HealthState.OK
            )
        except Exception:  # noqa: BLE001 - persistence must not mask dispatch success
            self._audit(
                "llm_gateway.health_update_failed",
                f"model_route:{route.alias}:{route.provider}:{route.model_ref}",
                {},
            )

    def _send_one(self, route: ModelRoute, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        adapter = self._adapters.adapter_for(route)
        response = adapter.send(request)
        _enforce_gateway_fields(response, route, request)
        response.latency_ms = max(0, round((time.monotonic() - started) * 1000))
        return response

    def dispatch(self, request: LLMRequest) -> LLMResponse:
        """Dispatch one request idempotently over the alias fallback chain."""
        cached = self._cache.get(request.execution_id)
        if cached is not None:
            return cached
        routes = self._eligible_routes(request.alias)
        if not routes:
            raise LLMDispatchError(f"no enabled routes for alias: {request.alias}")
        subject = f"execution:{request.execution_id}"
        self._audit(
            "llm_gateway.dispatch_started",
            subject,
            {"alias": request.alias, "routes": len(routes)},
        )
        from clipos_api.llm_cost import DispatchOutcome, DispatchRecord

        try:
            failures: list[dict[str, Any]] = []
            for route in routes:
                try:
                    response = self._send_one(route, request)
                except Exception as exc:  # noqa: BLE001 - fall through the chain
                    failures.append(
                        {
                            "provider": route.provider,
                            "model_ref": route.model_ref,
                            "error_class": type(exc).__name__,
                        }
                    )
                    self._degrade_route(route)
                    self._record(
                        DispatchRecord(
                            execution_id=request.execution_id,
                            alias=request.alias,
                            provider=route.provider,
                            model_ref=route.model_ref,
                            outcome=DispatchOutcome.FAILED,
                            latency_ms=0,
                            at=self._clock(),
                        )
                    )
                    continue
                if route.health_state is HealthState.DEGRADED:
                    self._recover_route(route)
                self._record(
                    DispatchRecord(
                        execution_id=request.execution_id,
                        alias=request.alias,
                        provider=route.provider,
                        model_ref=route.model_ref,
                        outcome=(
                            DispatchOutcome.DEGRADED_FALLBACK
                            if failures
                            else DispatchOutcome.SUCCEEDED
                        ),
                        latency_ms=response.latency_ms,
                        usage=response.usage,
                        at=self._clock(),
                    )
                )
                self._cache[request.execution_id] = response
                self._audit(
                    "llm_gateway.dispatch_succeeded",
                    subject,
                    {
                        "alias": request.alias,
                        "provider": route.provider,
                        "model_ref": route.model_ref,
                        "latency_ms": response.latency_ms,
                    },
                )
                return response
            raise LLMDispatchError(f"all routes failed for alias: {request.alias}") from None
        except BaseException as exc:
            self._audit(
                "llm_gateway.dispatch_failed",
                subject,
                {"alias": request.alias, "error_class": type(exc).__name__},
            )
            raise


def _enforce_gateway_fields(response: LLMResponse, route: ModelRoute, request: LLMRequest) -> None:
    """Gateways own routing fields; adapters never supply them."""
    response.alias = request.alias
    response.provider = route.provider
    response.model_ref = route.model_ref
    response.execution_id = request.execution_id
