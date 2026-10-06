import asyncio
import dataclasses
import os

import pytest

from clipos_api.temporal_config import TemporalConfig, TemporalProbeResult, probe_temporal


def test_defaults() -> None:
    config = TemporalConfig.from_env(
        address=None, namespace=None, ui_url=None, connect_timeout=None
    )
    assert config.address == "localhost:7233"
    assert config.namespace == "clipos-dev"
    assert config.ui_url == "http://localhost:8233"
    assert config.connect_timeout_seconds == 5.0


def test_env_overrides() -> None:
    config = TemporalConfig.from_env(
        address="temporal:7233",
        namespace="clipos-test",
        ui_url="http://temporal-ui:8088",
        connect_timeout="2.5",
    )
    assert config.address == "temporal:7233"
    assert config.namespace == "clipos-test"
    assert config.ui_url == "http://temporal-ui:8088"
    assert config.connect_timeout_seconds == 2.5


def test_empty_address_falls_back_to_default() -> None:
    assert TemporalConfig.from_env(address="").address == "localhost:7233"


@pytest.mark.parametrize(
    "bad",
    ["no-port", "host:notaport", "host:0", "host:70000", "http://host:7233", ":7233"],
)
def test_malformed_address_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="TEMPORAL_ADDRESS"):
        TemporalConfig.from_env(address=bad)


@pytest.mark.parametrize("bad", ["zero", "-1", "abc"])
def test_bad_timeout_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="TEMPORAL_CONNECT_TIMEOUT"):
        TemporalConfig.from_env(connect_timeout=bad)


def test_config_and_probe_result_frozen() -> None:
    config = TemporalConfig.from_env()
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.address = "other:7233"  # type: ignore[misc]
    result = TemporalProbeResult(ok=True, detail="ok")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = False  # type: ignore[misc]


def test_probe_unreachable_returns_sanitized_failure() -> None:
    config = TemporalConfig.from_env(address="127.0.0.1:1", connect_timeout="1")
    result = asyncio.run(probe_temporal(config))
    assert result.ok is False
    assert "Traceback" not in result.detail


@pytest.mark.skipif(not os.environ.get("TEMPORAL_ADDRESS"), reason="live Temporal not configured")
def test_probe_live_temporal() -> None:
    config = TemporalConfig.from_env()
    result = asyncio.run(probe_temporal(config))
    assert result.ok, result.detail
