"""Asset validation and quarantine tables (CLIP-039).

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-11

CREATE-only by policy: this migration introduces only the fresh validation
and quarantine tables. ``downgrade`` is therefore a no-op; fresh environments
start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the asset_validations and quarantine_records tables."""
    op.create_table(
        "asset_validations",
        sa.Column("validation_id", sa.String(length=36), nullable=False),
        sa.Column("media_asset_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("probe_ref", sa.String(length=512), nullable=True),
        sa.Column("reason", sa.String(length=512), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("validation_id"),
    )
    op.create_index(
        "ix_asset_validations_asset_org",
        "asset_validations",
        ["media_asset_id", "org_id"],
        unique=False,
    )
    op.create_index(
        "ix_asset_validations_validated_at",
        "asset_validations",
        ["validated_at"],
        unique=False,
    )

    op.create_table(
        "quarantine_records",
        sa.Column("quarantine_id", sa.String(length=36), nullable=False),
        sa.Column("media_asset_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("quarantine_id"),
    )
    op.create_index(
        "uq_quarantine_media_org_active",
        "quarantine_records",
        ["media_asset_id", "org_id"],
        unique=True,
    )
    op.create_index(
        "ix_quarantine_released_at",
        "quarantine_records",
        ["released_at"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""
