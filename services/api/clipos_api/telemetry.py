"""OpenTelemetry primitives for tracing, metrics, and redacted logging (CLIP-013).

Telemetry is initialized once per process. ``mode=none`` leaves the global
providers untouched so the OpenTelemetry API keeps working as a no-op, which is
the sandbox/development default. ``mode=console`` installs SDK providers that
export spans and metrics to stdout. A remote OTLP/Prometheus collector does not
exist yet; that exporter mode is deliberately out of scope until the
02 architecture's collector deployment ticket lands.

Every log handler wrapped by :func:`install_redaction` rewrites Bearer tokens,
``sk-``/AKIA-style API keys, URL credentials, and password/token query values
before records reach any sink, per 07/13.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_VALID_MODES = ("none", "console")
_REDACTED = "[REDACTED]"

_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")
_SK_RE = re.compile(r"sk-[A-Za-z0-9]{8,}")
_AKIA_RE = re.compile(r"AKIA[A-Z0-9]{16}")
_URL_CRED_RE = re.compile(r"(?<=://)[^@\s/]+:[^@\s/]+@")
_KV_RE = re.compile(r"(?i)\b(password|token|secret|api_key)=([^&\s\"']+)")


@dataclass(frozen=True)
class TelemetryConfig:
    """Static telemetry settings parsed from the environment."""

    service_name: str
    mode: str
    log_level: int

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> TelemetryConfig:
        env = os.environ if environ is None else environ
        mode = env.get("OTEL_MODE", "none").strip().lower()
        if mode not in _VALID_MODES:
            raise ValueError(f"OTEL_MODE must be one of {'|'.join(_VALID_MODES)}, got {mode!r}")
        level_name = env.get("OTEL_LOG_LEVEL", "INFO").upper()
        level = logging.getLevelNamesMapping().get(level_name)
        if level is None:
            raise ValueError(f"OTEL_LOG_LEVEL is not a valid level: {level_name!r}")
        return cls(
            service_name=env.get("OTEL_SERVICE_NAME", "clipos-api"),
            mode=mode,
            log_level=level,
        )


class SecretRedactionFilter(logging.Filter):
    """Masks credential-shaped text in log messages and arguments."""

    def _redact(self, value: str) -> str:
        value = _BEARER_RE.sub(f"Bearer {_REDACTED}", value)
        value = _SK_RE.sub(_REDACTED, value)
        value = _AKIA_RE.sub(_REDACTED, value)
        value = _URL_CRED_RE.sub("***:***@", value)
        return _KV_RE.sub(lambda match: f"{match.group(1)}={_REDACTED}", value)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self._redact(message)
        record.msg = redacted
        record.args = ()
        return True


class TelemetryHandle:
    """Tracer/meter factories plus shutdown for an initialized process."""

    def __init__(self, config: TelemetryConfig, provider: TracerProvider) -> None:
        self._config = config
        self._provider = provider
        self._shutdown = False

    @property
    def config(self) -> TelemetryConfig:
        return self._config

    def tracer(self, name: str) -> Any:
        return trace.get_tracer(name)

    def meter(self, name: str) -> Any:
        return metrics.get_meter(name)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        shutdown = getattr(self._provider, "shutdown", None)
        if callable(shutdown):
            shutdown()


def init_telemetry(config: TelemetryConfig | None = None) -> TelemetryHandle:
    """Configure telemetry once; later calls return the existing handle."""
    global _HANDLE
    if _HANDLE is not None:
        return _HANDLE
    resolved = config if config is not None else TelemetryConfig.from_env()
    provider: TracerProvider
    if resolved.mode == "console":
        provider = TracerProvider(resource=_service_resource(resolved.service_name))
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        metrics.set_meter_provider(
            MeterProvider(
                metric_readers=[
                    PeriodicExportingMetricReader(
                        ConsoleMetricExporter(), export_interval_millis=60_000
                    )
                ]
            )
        )
        trace.set_tracer_provider(provider)
    else:
        provider = cast_provider(trace.get_tracer_provider())
    _HANDLE = TelemetryHandle(resolved, provider)
    return _HANDLE


def _service_resource(service_name: str) -> Any:
    from opentelemetry.sdk.resources import Resource

    return Resource.create({"service.name": service_name})


def cast_provider(provider: Any) -> TracerProvider:
    """Accept the API's no-op/tracer-provider proxy as a usable provider value."""
    return provider  # type: ignore[return-value]


def get_handle() -> TelemetryHandle | None:
    return _HANDLE


def reset_handle() -> None:
    """Test-only: forget the process handle so a fresh mode can be installed."""
    global _HANDLE
    _HANDLE = None


def counter(name: str, unit: str = "1") -> Any:
    handle = _HANDLE
    if handle is None:
        return None
    meter = handle.meter("clipos_api.telemetry")
    return meter.create_counter(name, unit=unit)


def histogram(name: str, unit: str = "1") -> Any:
    handle = _HANDLE
    if handle is None:
        return None
    meter = handle.meter("clipos_api.telemetry")
    return meter.create_histogram(name, unit=unit)


def install_redaction(handler: logging.Handler | None = None) -> None:
    """Attach the redaction filter to a handler (default: root)."""
    target = handler if handler is not None else logging.getLogger()
    for existing in target.filters:
        if isinstance(existing, SecretRedactionFilter):
            return
    target.addFilter(SecretRedactionFilter())


_HANDLE: TelemetryHandle | None = None
