"""Telemetry primitive tests: init idempotence, redaction, no-op APIs (CLIP-013)."""

from __future__ import annotations

import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from clipos_api.telemetry import (
    SecretRedactionFilter,
    TelemetryConfig,
    TelemetryHandle,
    counter,
    get_handle,
    histogram,
    init_telemetry,
    install_redaction,
    reset_handle,
)


@pytest.fixture(autouse=True)
def _clean_telemetry():
    reset_handle()
    yield
    reset_handle()


def test_from_env_defaults_and_validation() -> None:
    config = TelemetryConfig.from_env({})
    assert config.service_name == "clipos-api"
    assert config.mode == "none"
    with pytest.raises(ValueError, match="OTEL_MODE"):
        TelemetryConfig.from_env({"OTEL_MODE": "collector"})


def test_from_env_rejects_unknown_log_level() -> None:
    with pytest.raises(ValueError, match="OTEL_LOG_LEVEL"):
        TelemetryConfig.from_env({"OTEL_LOG_LEVEL": "VERBOSE"})


def test_from_env_accepts_known_log_level() -> None:
    config = TelemetryConfig.from_env({"OTEL_LOG_LEVEL": "debug"})
    assert config.log_level == logging.DEBUG


def test_shutdown_is_idempotent_and_calls_provider_shutdown() -> None:
    calls: list[int] = []

    class _Provider:
        def shutdown(self) -> None:
            calls.append(1)

    handle = TelemetryHandle(TelemetryConfig.from_env({}), _Provider())  # type: ignore[arg-type]
    handle.shutdown()
    handle.shutdown()
    assert calls == [1]


def test_shutdown_tolerates_provider_without_shutdown() -> None:
    handle = TelemetryHandle(TelemetryConfig.from_env({}), object())  # type: ignore[arg-type]
    handle.shutdown()


def test_counter_and_histogram_return_instruments_after_init() -> None:
    handle = init_telemetry(TelemetryConfig.from_env({"OTEL_MODE": "none"}))
    assert handle is get_handle()
    assert counter("requests") is not None
    assert histogram("latency", unit="ms") is not None


def test_init_is_idempotent_in_none_mode() -> None:
    config = TelemetryConfig.from_env({"OTEL_MODE": "none"})
    first = init_telemetry(config)
    provider = trace.get_tracer_provider()
    second = init_telemetry(config)
    assert first is second is get_handle()
    assert trace.get_tracer_provider() is provider
    with first.tracer("test").start_span("noop") as span:
        span.set_attribute("ticket", "CLIP-013")
    assert first.meter("test") is not None


def test_counter_and_histogram_are_noop_safe_before_init() -> None:
    assert counter("requests", unit="1") is None
    assert histogram("latency", unit="ms") is None


def test_console_mode_initializes_sdk_providers() -> None:
    handle = init_telemetry(
        TelemetryConfig.from_env({"OTEL_MODE": "console", "OTEL_SERVICE_NAME": "clipos-test"})
    )
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert handle.config.service_name == "clipos-test"
    assert handle is init_telemetry()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Bearer abc.def.ghi", "Bearer [REDACTED]"),
        ("key sk-1234567890abcd done", "key [REDACTED] done"),
        (
            "id " + "AKIA" + "IOSFODNN" + "7EXAMPLE",
            "id [REDACTED]",
        ),
        ("https://user:pw@example.com/x", "https://***:***@example.com/x"),
        ("password=hunter2 status", "password=[REDACTED] status"),
        ("token=abc123&next=2", "token=[REDACTED]&next=2"),
    ],
)
def test_redaction_filter_masks_all_pattern_classes(raw: str, expected: str) -> None:
    filt = SecretRedactionFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=raw,
        args=None,
        exc_info=None,
    )
    assert filt.filter(record) is True
    assert record.getMessage() == expected


def test_redaction_preserves_normal_text_and_is_idempotent_on_handler() -> None:
    filt = SecretRedactionFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="all systems nominal",
        args=None,
        exc_info=None,
    )
    filt.filter(record)
    assert record.getMessage() == "all systems nominal"

    handler = logging.NullHandler()
    install_redaction(handler)
    install_redaction(handler)
    assert sum(isinstance(f, SecretRedactionFilter) for f in handler.filters) == 1
