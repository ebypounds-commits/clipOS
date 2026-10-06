"""Backup primitive tests: file manifests, tampering, and gated pg_dump (CLIP-013)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from clipos_api.backup import (
    BackupError,
    create_files_manifest,
    pg_dump_backup,
    verify_manifest,
)

CLI = Path(__file__).resolve().parents[3] / "scripts" / "backup.py"


def _make_files(tmp: Path) -> list[Path]:
    a = tmp / "a.txt"
    b = tmp / "sub"
    b.mkdir(parents=True)
    c = b / "b.txt"
    a.write_bytes(b"alpha")
    c.write_bytes(b"beta")
    return [a, c]


def test_create_and_verify_roundtrip(tmp_path: Path) -> None:
    files = _make_files(tmp_path / "src")
    dest = tmp_path / "backup"
    manifest = create_files_manifest(files, dest)
    assert manifest.kind == "files"
    assert manifest.total_bytes == 9
    assert (dest / "manifest.json").is_file()
    assert verify_manifest(dest / "manifest.json") == []


def test_tampered_and_missing_files_are_reported(tmp_path: Path) -> None:
    files = _make_files(tmp_path / "src")
    dest = tmp_path / "backup"
    manifest_path = create_files_manifest(files, dest).write(dest)
    (tmp_path / "src" / "a.txt").write_bytes(b"tampered")
    problems = verify_manifest(manifest_path)
    assert len(problems) == 1
    assert "sha256 mismatch" in problems[0]
    (tmp_path / "src" / "a.txt").unlink()
    problems = verify_manifest(manifest_path)
    assert problems == [f"missing file: {tmp_path / 'src' / 'a.txt'}"]


def test_missing_source_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="missing backup source file"):
        create_files_manifest([tmp_path / "nope.txt"], tmp_path / "out")


def test_cli_create_and_verify(tmp_path: Path) -> None:
    files = _make_files(tmp_path / "src")
    dest = tmp_path / "backup"
    created = subprocess.run(
        [sys.executable, str(CLI), "create", str(dest), *map(str, files)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(created.stdout)
    assert payload["kind"] == "files"
    verified = subprocess.run(
        [sys.executable, str(CLI), "verify", str(dest / "manifest.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0
    assert json.loads(verified.stdout) == {"ok": True, "problems": []}


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL required for pg_dump integration",
)
def test_pgdump_gated_integration(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    dest = tmp_path / "pg"
    manifest = pg_dump_backup(dest)
    dump_path = Path(manifest.items[0].path)
    assert dump_path.is_file() and dump_path.stat().st_size > 0
    assert manifest.kind == "postgres"
    assert manifest.write(dest).is_file()
    problems = verify_manifest(dest / "manifest.json")
    assert problems == []
    url = os.environ["DATABASE_URL"]
    logged = caplog.text + (dump_path.read_bytes()[:64].hex())
    assert url not in logged


def test_pgdump_requires_database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(BackupError, match="DATABASE_URL"):
        pg_dump_backup(tmp_path)


def test_pgdump_failure_redacts_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:sekret@localhost:9/db")
    with pytest.raises(BackupError) as excinfo:
        pg_dump_backup(tmp_path)
    message = str(excinfo.value)
    assert "sekret" not in message
    assert "postgresql://user:sekret@localhost:9/db" not in message


def test_normalize_libpq_url() -> None:
    from clipos_api.backup import _normalize_libpq_url

    assert (
        _normalize_libpq_url("postgresql+psycopg://user:pw@localhost:5433/db")
        == "postgresql://user:pw@localhost:5433/db"
    )
    assert _normalize_libpq_url("postgresql+psycopg2://u:p@h/d") == "postgresql://u:p@h/d"
    assert (
        _normalize_libpq_url("postgresql://user:pw@localhost:5433/db")
        == "postgresql://user:pw@localhost:5433/db"
    )
