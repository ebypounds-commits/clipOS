import re
from pathlib import Path

import pytest

from clipos_api import registry

PROVIDER_PATTERN = re.compile(r"gpt-|claude-|glm-|deepseek|gemini|llama-\d|mistral")


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


def test_no_hardcoded_provider_identifiers() -> None:
    source_root = Path(__file__).resolve().parents[1] / "clipos_api"
    for path in source_root.glob("*.py"):
        text = path.read_text()
        normalized = text if path.name != "providers.py" else "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        assert not PROVIDER_PATTERN.search(normalized), path.name
