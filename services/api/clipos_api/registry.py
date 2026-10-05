"""Logical provider registry.

Keys are stable logical aliases owned by configuration. Concrete vendor and model
identity is resolved by adapters at runtime.
"""
from collections.abc import Mapping
from typing import Any

_REGISTRY: Mapping[str, Any] = {}


def register(alias: str, provider: Any) -> None:
    if not alias or not alias.replace("-", "_").replace("_", "").isalnum():
        raise ValueError("invalid provider alias")
    if alias in _REGISTRY:
        raise ValueError(f"provider alias already registered: {alias}")
    _REGISTRY[alias] = provider


def get(alias: str) -> Any:
    if alias not in _REGISTRY:
        raise KeyError(f"unknown provider alias: {alias}")
    return _REGISTRY[alias]


def clear() -> None:
    _REGISTRY.clear()
