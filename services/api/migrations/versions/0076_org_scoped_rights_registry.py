"""Scope the source-item and provenance-manifest uniqueness to the org.

Revision ID: 0076
Revises: 0075
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def _drop_unique(table: str, name: str) -> None:
    """Drop a unique object that may be an index or a constraint.

    ``provenance_manifests`` carries its uniqueness as a UNIQUE CONSTRAINT
    (Postgres refuses to drop its index directly), while ``source_items``
    carries a plain unique index. The offline SQL renderer may run against
    the SQLite dialect, whose operations implementation refuses ALTER of
    constraints before any SQL can be produced, so the dialect decides
    which form is emitted.
    """
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(name, table, type_="unique")
    else:
        # DROP INDEX covers the plain index; for a constraint-owned index
        # the SQLite DDL would be a no-op, and SQLite is never the target
        # of these migrations.
        op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))


def upgrade() -> None:
    # Source items are org-scoped: two organizations may register the same
    # source item, and the ledger and provenance stores look an item up by
    # (item_id, org_id).
    _drop_unique("source_items", "uq_source_items_item_id")
    op.create_index(
        "uq_source_items_item_org",
        "source_items",
        ["item_id", "org_id"],
        unique=True,
    )
    # The manifest id is derived from the item id ("prov-<item_id>"), so it
    # is org-scoped for the same reason.
    _drop_unique("provenance_manifests", "uq_provenance_manifests_manifest_id")
    op.create_index(
        "uq_provenance_manifests_manifest_org",
        "provenance_manifests",
        ["manifest_id", "org_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_provenance_manifests_manifest_org", table_name="provenance_manifests")
    op.create_unique_constraint(
        "uq_provenance_manifests_manifest_id",
        "provenance_manifests",
        ["manifest_id"],
    )
    op.drop_index("uq_source_items_item_org", table_name="source_items")
    op.create_index(
        "uq_source_items_item_id",
        "source_items",
        ["item_id"],
        unique=True,
    )
