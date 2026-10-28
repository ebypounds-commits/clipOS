"""Worker bootstrap configuration tests (env-only, credential-free)."""

from __future__ import annotations

import pytest

from clipos_api.worker import WorkerConfig


def test_from_env_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPORAL_TASK_QUEUE", raising=False)
    monkeypatch.delenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", raising=False)
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    config = WorkerConfig.from_env()
    assert config.task_queue == "clipos-core"
    assert config.max_concurrent_activities == 100
    assert config.temporal.address == "localhost:7233"


def test_from_env_reads_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", "clipos-render")
    monkeypatch.setenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "7")
    config = WorkerConfig.from_env()
    assert config.task_queue == "clipos-render"
    assert config.max_concurrent_activities == 7


def test_explicit_arguments_win_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", "from-env")
    monkeypatch.setenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "3")
    config = WorkerConfig.from_env(task_queue="explicit", max_concurrent_activities="9")
    assert config.task_queue == "explicit"
    assert config.max_concurrent_activities == 9


def test_non_integer_concurrency_is_rejected() -> None:
    with pytest.raises(ValueError, match="TEMPORAL_MAX_CONCURRENT_ACTIVITIES"):
        WorkerConfig.from_env(max_concurrent_activities="many")


@pytest.mark.parametrize("raw", ["0", "-3"])
def test_non_positive_concurrency_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        WorkerConfig.from_env(max_concurrent_activities=raw)


def test_blank_concurrency_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "   ")
    config = WorkerConfig.from_env()
    assert config.max_concurrent_activities == 100


def test_config_never_carries_credentials() -> None:
    config = WorkerConfig.from_env()
    rendered = f"{config.temporal.address}|{config.temporal.namespace}"
    assert "password" not in rendered.lower()
    assert "token" not in rendered.lower()
