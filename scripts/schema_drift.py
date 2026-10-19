#!/usr/bin/env python3
"""Diff ORM-declared schema against the migrated Postgres schema (CLIP-100 follow-up).

CI always builds SQLite tables from the models (Base.metadata.create_all), so a
model/migration divergence passes CI and only surfaces against a real Postgres
created by alembic. Two live bugs were found this way:

* ``agent_versions.agent_ref`` was INSERTed before its ``agent_records`` parent
  existed (autoflush ordering) — 500 on first agent registration;
* ``EvaluationRunRecord`` declared an ``id`` primary key that migration 0006
  never created — ``column agent_evaluation_runs.id does not exist`` on
  activation queries.

Usage:
  DATABASE_URL=... python3 scripts/schema_drift.py
  python3 scripts/schema_drift.py postgresql+psycopg://...

Exit codes:
  0  models and database agree
  1  at least one divergence (a table or column the runtime will trip over)
  2  no database connection available
"""
from __future__ import annotations

import sys

import clipos_api.main  # noqa: F401  # registers every model module
from clipos_api.repository import Base
from sqlalchemy import create_engine, inspect, text

# Tables alembic maintains itself; never a drift signal.
_IGNORED_TABLES = {"alembic_version"}


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        url = argv[1]
    else:
        import os

        url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("usage: DATABASE_URL=... scripts/schema_drift.py [url]", file=sys.stderr)
        return 2

    engine = create_engine(url)
    inspector = inspect(engine)
    expected = {table.name: table for table in Base.metadata.sorted_tables}
    with engine.connect() as conn:
        actual = set(
            conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
            ).scalars()
        ) - _IGNORED_TABLES

    problems = 0

    missing_tables = sorted(set(expected) - actual)
    extra_tables = sorted(actual - set(expected))
    print("=== tables in models but not in Postgres ===")
    for name in missing_tables:
        print(f"  MISSING TABLE: {name}")
        problems += 1
    print("=== tables in Postgres but not in models ===")
    for name in extra_tables:
        print(f"  EXTRA TABLE: {name}")

    print("=== column divergence on shared tables ===")
    divergent = 0
    for name in sorted(set(expected) & actual):
        model_cols = {column.name for column in expected[name].columns}
        db_cols = {column["name"] for column in inspector.get_columns(name)}
        model_only = sorted(model_cols - db_cols)
        db_only = sorted(db_cols - model_cols)
        if model_only:
            divergent += 1
            problems += 1
            print(f"  {name}:")
            print(f"      model-only (breaks on Postgres): {model_only}")
            if db_only:
                print(f"      db-only (model never maps it): {db_only}")
        elif db_only:
            # Harmless: the model simply does not map an extra column.
            print(f"  {name}: db-only (model never maps it): {db_only}")

    print(
        f"\ntables checked={len(set(expected) & actual)} "
        f"missing_tables={len(missing_tables)} breaking_divergences={divergent}"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
