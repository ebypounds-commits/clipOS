"""Backup manifest primitives: hash files, verify, and pg_dump (CLIP-013).

These are primitives only: no scheduler, no encryption, and no object-storage
upload. The 12 runbook's "daily encrypted full backup" stays on the roadmap.
Manifest ``id`` and ``created_at`` differ between runs; item hashes are
deterministic for identical inputs, so re-running create is safe.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class BackupError(RuntimeError):
    """Raised when a backup source is missing or pg_dump fails."""


class ManifestItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    sha256: str
    size_bytes: int


class BackupManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int = 1
    id: str
    created_at: str
    kind: str
    items: list[ManifestItem] = Field(default_factory=list)
    total_bytes: int
    note: str | None = None

    def write(self, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = dest_dir / "manifest.json"
        manifest_path.write_text(self.model_dump_json(indent=2) + "\n")
        return manifest_path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_files_manifest(paths: Sequence[str | Path], dest_dir: str | Path) -> BackupManifest:
    """Hash every existing file and write ``manifest.json`` into dest_dir."""
    dest = Path(dest_dir)
    items: list[ManifestItem] = []
    for raw in paths:
        source = Path(raw)
        if not source.is_file():
            raise BackupError(f"missing backup source file: {source}")
        items.append(
            ManifestItem(
                path=str(source),
                sha256=_sha256_file(source),
                size_bytes=source.stat().st_size,
            )
        )
    manifest = BackupManifest(
        id=str(uuid4()),
        created_at=datetime.now(UTC).isoformat(),
        kind="files",
        items=items,
        total_bytes=sum(item.size_bytes for item in items),
    )
    manifest.write(dest)
    return manifest


def verify_manifest(manifest_path: str | Path) -> list[str]:
    """Return one problem line per tampered or missing file; [] when clean."""
    data = json.loads(Path(manifest_path).read_text())
    problems: list[str] = []
    for item in data.get("items", []):
        path = Path(item["path"])
        if not path.is_file():
            problems.append(f"missing file: {path}")
            continue
        actual = _sha256_file(path)
        if actual != item["sha256"]:
            problems.append(f"sha256 mismatch: {path} expected {item['sha256']} got {actual}")
    return problems


def _redact_url_text(text: str, database_url: str) -> str:
    """Remove the database URL (and embedded credentials) from any message."""
    pattern = (
        re.compile(r"//[^@\s]+@", re.IGNORECASE)
        if "://" in database_url
        else re.compile(r"(?i)database[_ ]?url=\S+")
    )
    redacted = pattern.sub("//***:***@", text)
    if database_url:
        redacted = redacted.replace(database_url, "[REDACTED]")
    return redacted


def _normalize_libpq_url(url: str) -> str:
    """Strip SQLAlchemy driver suffixes (postgresql+psycopg://) for libpq/pg_dump."""
    if url.startswith("postgresql+"):
        head, _, rest = url.partition("://")
        driver = head.split("+", 1)[0]
        return f"{driver}://{rest}"
    return url


def pg_dump_backup(dest_dir: str | Path, database_url: str | None = None) -> BackupManifest:
    """Run ``pg_dump --format=custom`` and manifest the dump file."""
    url = database_url if database_url is not None else os.environ.get("DATABASE_URL")
    if not url:
        raise BackupError("DATABASE_URL is required for pg_dump backup")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    dump_path = dest / "pg.dump"
    try:
        completed = subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                "--file",
                str(dump_path),
                _normalize_libpq_url(url),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except FileNotFoundError as error:
        raise BackupError("pg_dump binary is not installed in this environment") from error
    if completed.returncode != 0:
        stderr = _redact_url_text(completed.stderr, url)
        raise BackupError(f"pg_dump failed with exit {completed.returncode}: {stderr}")
    if not dump_path.is_file():
        raise BackupError("pg_dump reported success but the dump file is missing")
    items = [
        ManifestItem(
            path=str(dump_path),
            sha256=_sha256_file(dump_path),
            size_bytes=dump_path.stat().st_size,
        )
    ]
    manifest = BackupManifest(
        id=str(uuid4()),
        created_at=datetime.now(UTC).isoformat(),
        kind="postgres",
        items=items,
        total_bytes=items[0].size_bytes,
        note="pg_dump custom-format logical backup",
    )
    manifest.write(dest)
    return manifest


def _main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(argv) >= 2 and argv[0] == "create":
            dest, paths = argv[1], argv[2:]
            manifest = create_files_manifest(paths, dest)
        elif len(argv) == 2 and argv[0] == "verify":
            problems = verify_manifest(argv[1])
            print(json.dumps({"ok": not problems, "problems": problems}))
            return 0 if not problems else 1
        elif len(argv) == 2 and argv[0] == "pgdump":
            manifest = pg_dump_backup(argv[1])
        else:
            raise BackupError(
                "usage: backup.py create <dest_dir> [paths...] | verify <manifest.json> | "
                "pgdump <dest_dir>"
            )
        print(manifest.model_dump_json())
        return 0
    except BackupError as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised via scripts/backup.py
    raise SystemExit(_main())
