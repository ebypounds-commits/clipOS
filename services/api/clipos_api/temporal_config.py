"""Typed Temporal dev/self-host configuration and connectivity probe.

Configuration is environment-only; no credentials are configured or stored.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass

from temporalio.client import Client as TemporalClient

_ADDRESS_PATTERN = re.compile(r"^[A-Za-z0-9._-]+:[0-9]{1,5}$")
_DEFAULT_ADDRESS = "localhost:7233"
_DEFAULT_NAMESPACE = "clipos-dev"
_DEFAULT_UI_URL = "http://localhost:8233"
_DEFAULT_CONNECT_TIMEOUT = 5.0


@dataclass(frozen=True)
class TemporalConfig:
    """Connection settings for the dev Temporal frontend."""

    address: str
    namespace: str
    ui_url: str | None
    connect_timeout_seconds: float

    @classmethod
    def from_env(
        cls,
        *,
        address: str | None = None,
        namespace: str | None = None,
        ui_url: str | None = None,
        connect_timeout: str | None = None,
    ) -> TemporalConfig:
        """Build config from TEMPORAL_* variables (overridable for tests)."""
        raw_address = address if address is not None else os.environ.get("TEMPORAL_ADDRESS", "")
        raw_namespace = (
            namespace if namespace is not None else os.environ.get("TEMPORAL_NAMESPACE", "")
        )
        raw_ui_url = ui_url if ui_url is not None else os.environ.get("TEMPORAL_UI_URL", "")
        raw_timeout = (
            connect_timeout
            if connect_timeout is not None
            else os.environ.get("TEMPORAL_CONNECT_TIMEOUT", "")
        )
        return cls(
            address=_validate_address(raw_address),
            namespace=raw_namespace or _DEFAULT_NAMESPACE,
            ui_url=raw_ui_url or _DEFAULT_UI_URL,
            connect_timeout_seconds=_validate_timeout(raw_timeout),
        )


def _validate_address(raw: str) -> str:
    value = raw.strip()
    if not value:
        return _DEFAULT_ADDRESS
    if not _ADDRESS_PATTERN.match(value):
        raise ValueError(
            f"TEMPORAL_ADDRESS must be host:port (default {_DEFAULT_ADDRESS}); value malformed"
        )
    port = int(value.rpartition(":")[2])
    if not 1 <= port <= 65535:
        raise ValueError("TEMPORAL_ADDRESS port must be in 1..65535")
    return value


def _validate_timeout(raw: str) -> float:
    value = raw.strip()
    if not value:
        return _DEFAULT_CONNECT_TIMEOUT
    try:
        timeout = float(value)
    except ValueError as exc:
        raise ValueError("TEMPORAL_CONNECT_TIMEOUT must be a positive number") from exc
    if timeout <= 0:
        raise ValueError("TEMPORAL_CONNECT_TIMEOUT must be a positive number")
    return timeout


@dataclass(frozen=True)
class TemporalProbeResult:
    """Outcome of a bounded Temporal connectivity attempt."""

    ok: bool
    detail: str


async def probe_temporal(config: TemporalConfig) -> TemporalProbeResult:
    """Connect to Temporal within the configured timeout.

    Failures are reported as ``ok=False`` with a sanitized detail (exception
    class plus message; no tracebacks). No credentials are involved.
    """
    try:
        await asyncio.wait_for(
            TemporalClient.connect(config.address, namespace=config.namespace),
            timeout=config.connect_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - probe reports, never propagates
        return TemporalProbeResult(ok=False, detail=f"connect failed: {type(exc).__name__}: {exc}")
    return TemporalProbeResult(ok=True, detail=f"connected to {config.address}")
