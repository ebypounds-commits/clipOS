"""Startup registration failure handling for the platform publishing adapters.

Each platform adapter registers itself at import time. When registration
fails (for example because the publishing registry is unavailable), the
failure must be captured rather than raised during import, and the module
must expose it through ``_ensure_registered`` so startup can refuse to run
with a half-registered publishing surface.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from clipos_api import meta_adapter, publishing, tiktok_adapter, youtube_adapter

_ADAPTER_MODULES = (meta_adapter, tiktok_adapter, youtube_adapter)


@pytest.mark.parametrize("module", _ADAPTER_MODULES, ids=lambda m: m.__name__)
def test_successful_registration_records_no_error(module: Any) -> None:
    module._ensure_registered()
    assert module._register_error is None


@pytest.mark.parametrize("module", _ADAPTER_MODULES, ids=lambda m: m.__name__)
def test_registration_failure_is_captured_not_raised(
    module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(publishing, "register_adapter", _boom)
    reloaded = importlib.reload(module)
    try:
        assert reloaded._register_error is not None
        assert "registry unavailable" in reloaded._register_error
        with pytest.raises(RuntimeError, match="registration failed"):
            reloaded._ensure_registered()
    finally:
        monkeypatch.undo()
        importlib.reload(reloaded)


@pytest.mark.parametrize("module", _ADAPTER_MODULES, ids=lambda m: m.__name__)
def test_reload_after_failure_restores_healthy_registration(module: Any) -> None:
    reloaded = importlib.reload(module)
    assert reloaded._register_error is None
    reloaded._ensure_registered()
