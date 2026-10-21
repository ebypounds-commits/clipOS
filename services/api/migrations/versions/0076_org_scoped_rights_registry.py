"""Scope the source-item and provenance-manifest uniqueness to the org.

Revision ID: 0076
Revises: 0075
Create Date: 2026-10-21
"""

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Source items are org-scoped: two organizations may register the same
    # source item, and the ledger and provenance stores look an item up by
    # (item_id, org_id).
    op.drop_index("uq_source_items_item_id", table_name="source_items")
    op.create_index(
        "uq_source_items_item_org",
        "source_items",
        ["item_id", "org_id"],
        unique=True,
    )
    # The manifest id is derived from the item id ("prov-<item_id>"), so it
    # is org-scoped for the same reason.
    # This one is a UNIQUE CONSTRAINT in Postgres, not a plain index.
    op.drop_constraint(
        "uq_provenance_manifests_manifest_id",
        "provenance_manifests",
        type_="unique",
    )
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
