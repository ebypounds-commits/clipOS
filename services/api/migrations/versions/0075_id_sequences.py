"""attach id sequences to the raw-DDL tables (0074 -> 0075).

Seven tables were created by raw-DDL migrations (0012 rights ledger, 0013
provenance) with ``id INTEGER NOT NULL`` and no server default. SQLite
treats ``INTEGER PRIMARY KEY`` as a rowid alias and fills it in; Postgres
does not. The ORM declares those ids as autoincrement, so it omits the
column from the INSERT and lets the database supply it — against a real
Postgres every such insert failed with

    psycopg.errors.NotNullViolation: null value in column "id" of relation
    "rights_records" violates not-null constraint

so registering a rights record, a license, license evidence, an attribution
requirement, a content fingerprint, a rights event or a provenance manifest
returned 500. The test suite never saw it: CI builds its tables from the ORM
models (SQLite), where the id is filled in automatically, and the few
Postgres-backed tests did not touch these tables.

This migration attaches the missing sequences. It is additive: no column is
dropped or rewritten, existing rows keep their ids, and each sequence starts
above the current maximum. It is the one intentional exception to the
repository's create-only migration policy, because the alternative is a
schema on which the rights ledger cannot be written at all.

Revision ID: 0075
Revises: 0074
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None

# Tables whose integer primary key was created without a server default.
_SEQUENCE_TABLES = (
    "attribution_requirements",
    "content_fingerprints",
    "license_evidence",
    "licenses",
    "provenance_manifests",
    "rights_events",
    "rights_records",
)


def upgrade() -> None:
    for table in _SEQUENCE_TABLES:
        sequence = f"{table}_id_seq"
        op.execute(sa.text(f"CREATE SEQUENCE IF NOT EXISTS {sequence}"))
        op.execute(
            sa.text(f"ALTER TABLE {table} ALTER COLUMN id SET DEFAULT nextval('{sequence}')")
        )
        op.execute(sa.text(f"ALTER SEQUENCE {sequence} OWNED BY {table}.id"))
        # Start above the current maximum so the next insert cannot collide.
        op.execute(
            sa.text(
                f"SELECT setval('{sequence}', "
                f"GREATEST(COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, 1), false)"
            )
        )


def downgrade() -> None:
    for table in _SEQUENCE_TABLES:
        sequence = f"{table}_id_seq"
        op.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN id DROP DEFAULT"))
        op.execute(sa.text(f"DROP SEQUENCE IF EXISTS {sequence}"))
