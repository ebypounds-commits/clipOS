"""Scope the creator, source and fingerprint registries to the organization.

Revision ID: 0077
Revises: 0076
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def _drop_unique(table_name: str, name: str) -> None:
    """Drop a uniqueness object whether it is an index or a constraint.

    The same uniqueness is expressed as a plain unique index in some tables
    and as a unique constraint in others, so the object type is inspected
    rather than assumed. During offline SQL generation the bind may be a
    mock connection that does not support inspection; in that case we
    attempt both operations and let the database report the error if
    neither exists.
    """
    try:
        inspector = sa.inspect(op.get_bind())
        constraints = {c["name"] for c in inspector.get_unique_constraints(table_name)}
        indexes = {i["name"] for i in inspector.get_indexes(table_name)}
        if name in constraints:
            op.drop_constraint(name, table_name, type_="unique")
        elif name in indexes:
            op.drop_index(name, table_name=table_name)
        else:
            raise RuntimeError(f"{table_name}.{name} is not present; refusing to guess")
    except sa.exc.NoInspectionAvailable:
        # Offline mode: try both, let the DB report if neither exists.
        try:
            op.drop_constraint(name, table_name, type_="unique")
        except Exception:  # noqa: BLE001 - offline mode: fall back to the index form
            op.drop_index(name, table_name=table_name)


def upgrade() -> None:
    # Creators, sources, items and fingerprints were unique across the whole
    # installation, so one organization could not register an identifier that
    # another organization already held, and the registries looked identifiers
    # up without an org filter. Every org-scoped registry in ClipOS is keyed
    # by (identifier, org_id); these now match.
    _drop_unique("creators", "uq_creators_creator_id")
    op.create_index(
        "uq_creators_creator_org",
        "creators",
        ["creator_id", "org_id"],
        unique=True,
    )
    _drop_unique("content_sources", "uq_content_sources_source_id")
    op.create_index(
        "uq_content_sources_source_org",
        "content_sources",
        ["source_id", "org_id"],
        unique=True,
    )
    _drop_unique("source_items", "uq_source_items_source_external_ref")
    op.create_index(
        "uq_source_items_source_external_org",
        "source_items",
        ["source_id", "external_ref", "org_id"],
        unique=True,
    )
    _drop_unique("content_fingerprints", "uq_content_fingerprints_fingerprint_id")
    op.create_index(
        "uq_content_fingerprints_fingerprint_org",
        "content_fingerprints",
        ["fingerprint_id", "org_id"],
        unique=True,
    )
    # The manifest id is derived from the item id, so it is org-scoped too;
    # without the org column two organizations holding the same manifest id
    # would collide on the (manifest, algorithm) pair.
    _drop_unique("content_fingerprints", "uq_content_fingerprints_manifest_algorithm")
    op.create_index(
        "uq_content_fingerprints_manifest_algorithm_org",
        "content_fingerprints",
        ["manifest_id", "algorithm", "org_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_content_fingerprints_manifest_algorithm_org",
        table_name="content_fingerprints",
    )
    op.create_unique_constraint(
        "uq_content_fingerprints_manifest_algorithm",
        "content_fingerprints",
        ["manifest_id", "algorithm"],
    )
    op.drop_index(
        "uq_content_fingerprints_fingerprint_org",
        table_name="content_fingerprints",
    )
    op.create_unique_constraint(
        "uq_content_fingerprints_fingerprint_id",
        "content_fingerprints",
        ["fingerprint_id"],
    )
    op.drop_index(
        "uq_source_items_source_external_org",
        table_name="source_items",
    )
    op.create_index(
        "uq_source_items_source_external_ref",
        "source_items",
        ["source_id", "external_ref"],
        unique=True,
    )
    op.drop_index("uq_content_sources_source_org", table_name="content_sources")
    op.create_index(
        "uq_content_sources_source_id",
        "content_sources",
        ["source_id"],
        unique=True,
    )
    op.drop_index("uq_creators_creator_org", table_name="creators")
    op.create_index(
        "uq_creators_creator_id",
        "creators",
        ["creator_id"],
        unique=True,
    )
