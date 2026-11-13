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
    carries a plain unique index. The form differs per table, so it is
    inspected rather than assumed: assuming the wrong one fails with
    UndefinedObject on a fresh database, which is exactly how this
    migration broke CI after the dev database had already been upgraded
    by an index-based version of it. During offline SQL generation the
    bind is a mock connection that cannot inspect, so both forms are
    attempted and the database reports the error if neither exists - the
    pattern migration 0077 already uses.
    """
    try:
        inspector = sa.inspect(op.get_bind())
        constraints = {c["name"] for c in inspector.get_unique_constraints(table)}
        indexes = {i["name"] for i in inspector.get_indexes(table)}
        if name in constraints:
            op.drop_constraint(name, table, type_="unique")
        elif name in indexes:
            op.drop_index(name, table_name=table)
        else:
            raise RuntimeError(f"{table}.{name} is not present; refusing to guess")
    except sa.exc.NoInspectionAvailable:
        try:
            op.drop_constraint(name, table, type_="unique")
        except Exception:  # noqa: BLE001 - offline: fall back to the index form
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
