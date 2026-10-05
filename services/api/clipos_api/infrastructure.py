"""Typed health contract for the local Compose infrastructure."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

HealthStatus = Literal["healthy", "unhealthy", "starting", "absent"]


class ServiceHealth(BaseModel):
    """Health for one infrastructure service."""

    name: str
    status: HealthStatus
    detail: str | None = None
    checked_at: datetime


class InfrastructureHealth(BaseModel):
    """Aggregated infrastructure health."""

    services: list[ServiceHealth] = Field(default_factory=list)
    overall: Literal["healthy", "degraded"] = "degraded"

    @classmethod
    def model_validate_services(cls, services: list[ServiceHealth]) -> InfrastructureHealth:
        """Build an instance and compute its overall status."""
        overall: Literal["healthy", "degraded"] = (
            "healthy" if services and all(service.status == "healthy" for service in services) else "degraded"
        )
        return cls(services=services, overall=overall)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("missing or invalid timestamp")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _status_from_compose(entry: dict[str, object]) -> HealthStatus:
    if "Health" in entry:
        health = entry["Health"]
        if not isinstance(health, dict):
            return "starting"
        state = health.get("Status")
        if state == "healthy":
            return "healthy"
        if state == "unhealthy":
            return "unhealthy"
        if state == "starting":
            return "starting"
        return "starting"
    if "State" not in entry:
        return "absent"
    return "starting" if entry["State"] == "running" else "unhealthy"


def parse_compose_json(raw: str) -> InfrastructureHealth:
    """Parse newline-delimited ``docker compose ps --format json`` output."""
    checked = datetime.now(UTC)
    services: list[ServiceHealth] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if not isinstance(entry, dict) or not entry.get("Service"):
            raise ValueError("invalid compose ps record")
        try:
            service = ServiceHealth(
                name=str(entry["Service"]),
                status=_status_from_compose(entry),
                detail=str(entry["Status"]) if entry.get("Status") is not None else None,
                checked_at=_parse_timestamp(entry["CreatedAt"]) if entry.get("CreatedAt") else checked,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid compose ps record for {entry.get('Service')!r}") from exc
        services.append(service)
    return InfrastructureHealth.model_validate_services(services)
