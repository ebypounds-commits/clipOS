#!/usr/bin/env python3
"""Backup manifest CLI: create, verify, pgdump (CLIP-013).

Examples:
  python scripts/backup.py create backups/ file1.txt file2.txt
  python scripts/backup.py verify backups/manifest.json
  DATABASE_URL=... python scripts/backup.py pgdump backups/
"""

from __future__ import annotations

import sys
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[1] / "services" / "api"
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from clipos_api.backup import _main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_main())
