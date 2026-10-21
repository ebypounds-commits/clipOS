"""Tests for the code-under-test integrity guard.

The guard exists because a shared virtualenv can map ``clipos_api`` to a
different checkout than the one holding the tests; a run then reports on the
wrong artifact. These tests pin both directions: the guard accepts the
current checkout and refuses a foreign one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_CONFTEST_PATH = Path(__file__).resolve().parent / "conftest.py"


def _load_conftest():
    """Load conftest.py by path (pytest does not expose it as an import)."""
    spec = importlib.util.spec_from_file_location("clipos_conftest_guard", _CONFTEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["clipos_conftest_guard"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("clipos_conftest_guard", None)
        raise
    return module


def test_guard_accepts_the_checkout_under_test() -> None:
    """The package imported by this run must be the one in this tree."""
    module = _load_conftest()
    expected = Path(__file__).resolve().parents[1] / "clipos_api"
    assert module.check_code_under_test(expected, expected) is None


def test_guard_refuses_a_foreign_checkout() -> None:
    """A package imported from elsewhere must be reported, not tolerated."""
    module = _load_conftest()
    expected = Path(__file__).resolve().parents[1] / "clipos_api"
    foreign = Path("/srv/clipos/clipOS/services/api/clipos_api")
    message = module.check_code_under_test(expected, foreign)
    assert message is not None
    assert "different checkout" in message
    assert str(expected) in message
    assert str(foreign) in message
    assert "PYTHONPATH" in message


def test_guard_ignores_an_unimported_package() -> None:
    """No package imported yet is not a mismatch."""
    module = _load_conftest()
    expected = Path(__file__).resolve().parents[1] / "clipos_api"
    assert module.check_code_under_test(expected, None) is None
