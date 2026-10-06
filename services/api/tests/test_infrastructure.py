import json
from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter

from clipos_api.infrastructure import (
    InfrastructureHealth,
    ServiceHealth,
    parse_compose_json,
)


def compose_line(
    name: str,
    state: str = "running",
    health: object = None,
    status: str = "Up",
    created: str = "2026-10-06T12:00:00.000000000Z",
) -> str:
    entry: dict[str, object] = {
        "Service": name,
        "State": state,
        "Status": status,
        "CreatedAt": created,
    }
    if health is not None:
        entry["Health"] = health
    return json.dumps(entry)


def test_parse_healthy_running_and_absent() -> None:
    result = parse_compose_json(
        "\n".join(
            [
                compose_line(
                    "postgres",
                    health={"Status": "healthy", "FailingStreak": 0},
                    status="Up 1 minute (healthy)",
                ),
                compose_line("redis", state="running", status="Up 1 minute"),
            ]
        )
    )
    assert [service.status for service in result.services] == ["healthy", "starting"]
    assert result.overall == "degraded"
    assert all(isinstance(service.checked_at, datetime) for service in result.services)


def test_parse_unhealthy_marks_overall_degraded() -> None:
    result = parse_compose_json(
        "\n".join(
            [
                compose_line("postgres", health={"Status": "healthy"}, status="Up (healthy)"),
                compose_line(
                    "redis",
                    health={"Status": "unhealthy", "FailingStreak": 2},
                    status="Up (unhealthy)",
                ),
            ]
        )
    )
    assert result.overall == "degraded"
    assert result.services[1].status == "unhealthy"
    assert result.services[1].detail == "Up (unhealthy)"


def test_parse_missing_and_multiple_records() -> None:
    result = parse_compose_json("\n".join([compose_line("postgres"), compose_line("redis")]))
    assert [service.name for service in result.services] == ["postgres", "redis"]
    assert result.overall == "degraded"
    absent = parse_compose_json("")
    assert absent.services == []
    assert absent.overall == "degraded"


def test_schema_round_trip() -> None:
    checked = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    health = InfrastructureHealth.model_validate_services(
        [
            ServiceHealth(name="postgres", status="healthy", detail="Up", checked_at=checked),
            ServiceHealth(name="redis", status="starting", detail="starting", checked_at=checked),
        ]
    )
    adapter = TypeAdapter(InfrastructureHealth)
    restored = adapter.validate_json(adapter.dump_json(health))
    assert restored == health
    assert restored.overall == "degraded"


def test_invalid_record_raises() -> None:
    with pytest.raises(ValueError):
        parse_compose_json('{"State":"running"}')


def test_no_service_all_healthy_is_degraded() -> None:
    health = InfrastructureHealth.model_validate_services([])
    assert health.overall == "degraded"
