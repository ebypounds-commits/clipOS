import json
from pathlib import Path

import pytest

from clipos_api import audit


def test_audit_record_is_parseable_and_complete(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(audit, "AUDIT_PATH", tmp_path / "audit.log")
    event = audit.record(
        "state.test",
        "codex",
        "test-subject",
        {"result": "created"},
    )
    raw = (tmp_path / "audit.log").read_text().strip().splitlines()
    assert len(raw) == 1
    parsed = json.loads(raw[0])
    assert parsed == event
    for field in ("timestamp", "action", "actor", "subject", "details"):
        assert field in parsed


def test_audit_requires_identity_fields(monkeypatch) -> None:
    with pytest.raises(ValueError):
        audit.record("", "codex", "subject", {})


def test_record_creates_the_audit_directory(tmp_path: Path, monkeypatch) -> None:
    """The log directory is created on demand.

    The container image runs the API as an unprivileged user; an audit log
    whose directory does not exist yet would fail every audited action.
    """
    target = tmp_path / "nested" / "audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", target)
    audit.record("state.test", "codex", "test-subject", {"result": "created"})
    assert target.exists()
    assert json.loads(target.read_text().strip())["action"] == "state.test"


def test_audit_directory_is_configurable(monkeypatch) -> None:
    """A deployment can point the audit log at a directory it owns."""
    monkeypatch.setenv("AUDIT_DIR", "/tmp/clipos-audit")
    import importlib

    reloaded = importlib.reload(audit)
    try:
        assert reloaded.AUDIT_DIR == Path("/tmp/clipos-audit")
        assert reloaded.AUDIT_PATH == Path("/tmp/clipos-audit/audit.log")
    finally:
        monkeypatch.delenv("AUDIT_DIR", raising=False)
        importlib.reload(audit)
