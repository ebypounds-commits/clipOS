"""Regression tests for the migrated Postgres schema.

CI builds its tables from the ORM models (SQLite), so a divergence between
the models and the alembic-managed schema is invisible to the suite. Two
such divergences shipped and were only found when the qualification
harness ran against a real Postgres:

* seven tables created by raw-DDL migrations had an integer primary key
  with no server default, so every insert that omitted the id (which is
  what the ORM does for an autoincrement key) failed with
  NotNullViolation — registering a rights record returned 500;
* the rights registry keyed source items globally, so one organization
  could not register an item another organization had already ingested.

These tests run only when DATABASE_URL points at a migrated Postgres, and
they leave no data behind: every write is rolled back.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="needs a migrated Postgres (DATABASE_URL)",
)


@pytest.fixture()
def pg_engine():
    return create_engine(DATABASE_URL)


def test_every_autoincrement_primary_key_has_a_server_default(pg_engine) -> None:
    """An autoincrement key needs a database default on Postgres.

    Only integer keys are autoincrement; a text key is supplied by the
    application and must not have a default.
    """
    missing: list[str] = []
    inspector = inspect(pg_engine)
    with pg_engine.connect() as conn:
        for table in inspector.get_table_names():
            columns = {c["name"]: c for c in inspector.get_columns(table)}
            primary_key = inspector.get_pk_constraint(table) or {}
            for column in primary_key.get("constrained_columns") or []:
                if columns.get(column, {}).get("type", None).__class__.__name__ != "INTEGER":
                    continue
                default = conn.execute(
                    text(
                        "SELECT column_default FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :table "
                        "AND column_name = :column"
                    ),
                    {"table": table, "column": column},
                ).scalar()
                if default is None:
                    missing.append(f"{table}.{column}")
    assert not missing, f"primary keys without a server default: {missing}"


def test_registering_a_rights_record_works_against_postgres(pg_engine) -> None:
    """The operation that returned 500 must succeed, id and all."""
    from clipos_api.rights_ledger import RightsRecordRecord

    with pg_engine.connect() as conn:
        transaction = conn.begin()
        session = Session(bind=conn)
        try:
            row = RightsRecordRecord(
                record_id="pg-probe-record",
                item_id="pg-probe-item",
                org_id="pg-probe-org",
                status="REVIEW_REQUIRED",
                basis="OWNED",
                reviewed_by="reviewer:test",
                record_metadata={},
                created_at=datetime.now(UTC),
            )
            session.add(row)
            session.flush()
            assert row.id is not None, "the database must supply the id"
        finally:
            session.close()
            transaction.rollback()


def test_two_organizations_may_register_the_same_source_item(pg_engine) -> None:
    """The rights registry is org-scoped: ids may repeat across orgs."""
    from clipos_api.rights_entities import SourceItemRecord

    with pg_engine.connect() as conn:
        transaction = conn.begin()
        session = Session(bind=conn)
        try:
            for org in ("pg-org-a", "pg-org-b"):
                session.add(
                    SourceItemRecord(
                        item_id="pg-shared-item",
                        org_id=org,
                        source_id=f"pg-source-{org}",
                        external_ref="pg-ext",
                        captured_at=datetime.now(UTC),
                        content_ref="storage://pg-shared-item",
                        item_metadata={},
                        created_at=datetime.now(UTC),
                    )
                )
            session.flush()
        finally:
            session.close()
            transaction.rollback()
