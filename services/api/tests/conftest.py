"""Shared test fixtures and the code-under-test integrity guard.

Two responsibilities live here.

**1. Integrity guard.** The build host keeps one shared virtualenv whose
editable install of ``clipos_api`` is mapped to the canonical checkout. A
test run inside a worktree therefore imports the *canonical* product code
and silently reports a result about the wrong artifact — a green (or red)
result that says nothing about the branch under test. The guard refuses to
run when the imported package does not live in the checkout that owns these
tests, so the mistake is loud instead of silent.

Override with ``CLIPOS_ALLOW_FOREIGN_CODE=1`` only when deliberately running
against an installed package.

**2. Rights clearance.** ``clear_rights`` places an item in the CLEAR rights
state with the provenance manifest the publication gate requires, using the
shared helper in ``rights_helpers``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED_PACKAGE_DIR = _PACKAGE_ROOT / "clipos_api"
_ALLOW_ENV = "CLIPOS_ALLOW_FOREIGN_CODE"


def _imported_package_dir() -> Path | None:
    """Return the directory of the imported ``clipos_api`` package."""
    try:
        import clipos_api
    except Exception:  # noqa: BLE001 - the suite cannot run without the package
        return None
    origin = getattr(clipos_api, "__file__", None)
    if not origin:
        return None
    return Path(origin).resolve().parent


def check_code_under_test(expected_dir: Path, imported_dir: Path | None) -> str | None:
    """Return an error message when *imported_dir* is not *expected_dir*.

    Kept separate from the pytest hook so it can be unit-tested directly.
    """
    if imported_dir is None:
        return None
    if imported_dir == expected_dir:
        return None
    return (
        "clipos_api was imported from a different checkout than the one under test.\n"
        f"  expected: {expected_dir}\n"
        f"  imported: {imported_dir}\n"
        "  The product code under test is not the code in this tree; any result "
        "would describe another checkout.\n"
        "  fix: export PYTHONPATH=<checkout>/services/api  (or reinstall the "
        "editable package inside this checkout with `pip install -e services/api`)"
    )


def pytest_collection_finish(session: pytest.Session) -> None:
    """Abort the run when the imported package is not the one under test."""
    if os.environ.get(_ALLOW_ENV):
        return
    message = check_code_under_test(_EXPECTED_PACKAGE_DIR, _imported_package_dir())
    if message:
        raise pytest.UsageError(message)


@pytest.fixture()
def clear_rights(engine: Engine) -> Callable[..., str]:
    """Return a helper that places an item in the CLEAR rights state.

    The returned callable takes the source item id and returns the rights
    record id, so a test can revoke the record later. A publication of that
    item is then allowed; the same item without this call is denied.
    """
    from rights_helpers import clear_rights_for

    def _clear(
        item_id: str,
        *,
        org_id: str = "org-1",
        content_ref: str | None = None,
        record_id: str | None = None,
    ) -> str:
        return clear_rights_for(
            engine,
            item_id,
            org_id=org_id,
            content_ref=content_ref,
            record_id=record_id,
        )

    return _clear
