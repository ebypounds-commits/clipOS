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


def test_parse_string_health_field() -> None:
    """Newer engines report ``Health`` as a plain string."""
    entry = json.dumps({"Service": "postgres", "State": "running", "Health": "healthy"})
    result = parse_compose_json(entry)
    assert result.services[0].status == "healthy"
    assert result.overall == "healthy"


def test_parse_unknown_health_state_is_unhealthy() -> None:
    entry = json.dumps({"Service": "postgres", "State": "running", "Health": "degraded-ish"})
    result = parse_compose_json(entry)
    assert result.services[0].status == "unhealthy"
    assert result.overall == "degraded"


def test_parse_exited_service_is_unhealthy() -> None:
    entry = json.dumps({"Service": "worker", "State": "exited"})
    result = parse_compose_json(entry)
    assert result.services[0].status == "unhealthy"


def test_parse_empty_state_without_service_name_is_absent() -> None:
    """A record identified only by ``Name`` and without engine state is absent."""
    entry = json.dumps({"Name": "orphan", "State": ""})
    result = parse_compose_json(entry)
    assert result.services[0].name == "orphan"
    assert result.services[0].status == "absent"


def test_parse_json_array_form() -> None:
    """``docker compose ps --format json`` may emit a JSON array."""
    payload = json.dumps([json.loads(compose_line("postgres", health="healthy"))])
    result = parse_compose_json(payload)
    assert [service.name for service in result.services] == ["postgres"]
    assert result.services[0].status == "healthy"


def test_parse_json_array_with_encoded_strings() -> None:
    """Array entries may themselves be JSON strings."""
    payload = json.dumps([compose_line("redis")])
    result = parse_compose_json(payload)
    assert [service.name for service in result.services] == ["redis"]


def test_parse_ndjson_skips_blank_lines() -> None:
    raw = "\n\n" + compose_line("postgres") + "\n   \n"
    result = parse_compose_json(raw)
    assert [service.name for service in result.services] == ["postgres"]


def test_parse_ndjson_with_encoded_entry_string() -> None:
    """A line may be a JSON string that itself encodes the record."""
    raw = json.dumps(compose_line("redis"))
    result = parse_compose_json(raw)
    assert [service.name for service in result.services] == ["redis"]


def test_parse_uses_name_when_service_field_missing() -> None:
    entry = json.dumps({"Name": "minio", "State": "running"})
    result = parse_compose_json(entry)
    assert result.services[0].name == "minio"
    assert result.services[0].status == "starting"


def test_from_services_requires_every_service_healthy() -> None:
    health = InfrastructureHealth.from_services([ServiceHealth(name="postgres", status="healthy")])
    assert health.overall == "healthy"
