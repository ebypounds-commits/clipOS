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
