"""Typed infrastructure health contracts for the ClipOS dev stack.

Parses `docker compose ps --format json` (NDJSON, one object per line).
Observed real shape (docker compose v5.3.1): `Health` is a STRING
("healthy"/"unhealthy"/"starting") or absent; older engines may ship it as
a dict with a `Status` key. Both are accepted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

ServiceStatus = Literal["healthy", "unhealthy", "starting", "absent"]
OverallStatus = Literal["healthy", "degraded"]


class ServiceHealth(BaseModel):
    name: str
    status: ServiceStatus
    detail: str | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InfrastructureHealth(BaseModel):
    services: list[ServiceHealth]
    overall: OverallStatus

    @classmethod
    def from_services(cls, services: list[ServiceHealth]) -> InfrastructureHealth:
        healthy = bool(services) and all(s.status == "healthy" for s in services)
        return cls(services=services, overall="healthy" if healthy else "degraded")

    @classmethod
    def model_validate_services(cls, services: list[ServiceHealth]) -> InfrastructureHealth:
        """Contract constructor: validate a service list and compute `overall`."""
        return cls.from_services(list(services))


def _normalize_health(entry: dict) -> ServiceStatus:
    raw_health = entry.get("Health")
    if isinstance(raw_health, dict):
        state = raw_health.get("Status")
    elif isinstance(raw_health, str):
        state = raw_health or None
    else:
        state = None

    if state in ("healthy", "unhealthy", "starting"):
        return state  # type: ignore[return-value]

    if state is None:
        engine_state = entry.get("State")
        if engine_state in (None, "") and entry.get("Service") is None:
            return "absent"
        if engine_state == "running":
            return "starting"
        if engine_state in (None, ""):
            return "absent"
        return "unhealthy"
    return "unhealthy"


def parse_compose_json(raw: str) -> InfrastructureHealth:
    """Parse NDJSON (or a JSON array) from `docker compose ps --format json`."""
    services: list[ServiceHealth] = []
    text = (raw or "").strip()
    if not text:
        return InfrastructureHealth.from_services([])

    entries: list[dict] = []
    if text.startswith("["):
        loaded = json.loads(text)
        entries = [json.loads(x) if isinstance(x, str) else x for x in loaded]
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if isinstance(entry, str):
                entry = json.loads(entry)
            entries.append(entry)

    for entry in entries:
        if entry.get("Service") is None and entry.get("Name") is None:
            raise ValueError(f"compose record missing Service/Name: {entry!r}")
        status = _normalize_health(entry)
        services.append(
            ServiceHealth(
                name=str(entry.get("Service") or entry.get("Name") or "unknown"),
                status=status,
                detail=entry.get("Status"),
            )
        )
    return InfrastructureHealth.from_services(services)
