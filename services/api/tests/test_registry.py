import re
from pathlib import Path

import pytest

from clipos_api import registry

PROVIDER_PATTERN = re.compile(r"gpt-|claude-|glm-|deepseek|gemini|llama-\d|mistral")


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Keep the process-wide alias registry isolated per test."""
    saved = dict(registry._REGISTRY)
    registry.clear()
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


def test_unknown_alias_is_rejected() -> None:
    with pytest.raises(KeyError):
        registry.get("not-configured")


def test_register_and_retrieve() -> None:
    marker = object()
    registry.register("primary_logical", marker)
    assert registry.get("primary_logical") is marker


def test_duplicate_alias_rejected() -> None:
    registry.register("repeatable", object())
    with pytest.raises(ValueError):
        registry.register("repeatable", object())


@pytest.mark.parametrize("alias", ["", "bad alias", "alias!", "with space"])
def test_invalid_alias_is_rejected(alias: str) -> None:
    """Only alphanumeric aliases (optionally hyphen/underscore separated) register."""
    with pytest.raises(ValueError, match="invalid provider alias"):
        registry.register(alias, object())


@pytest.mark.parametrize("alias", ["simple", "with-hyphen", "with_underscore", "a1"])
def test_valid_alias_is_accepted(alias: str) -> None:
    marker = object()
    registry.register(alias, marker)
    assert registry.get(alias) is marker


def test_clear_removes_every_alias() -> None:
    registry.register("temporary_alias", object())
    registry.clear()
    with pytest.raises(KeyError):
        registry.get("temporary_alias")


def test_no_hardcoded_provider_identifiers() -> None:
    source_root = Path(__file__).resolve().parents[1] / "clipos_api"
    for path in source_root.glob("*.py"):
        text = path.read_text()
        normalized = (
            text
            if path.name != "providers.py"
            else "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        )
        assert not PROVIDER_PATTERN.search(normalized), path.name
