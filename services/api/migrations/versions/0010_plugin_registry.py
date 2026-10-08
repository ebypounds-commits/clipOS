"""Plugin Registry: CREATE-only table.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-08

CREATE-only by policy: this migration introduces only the fresh plugin
registry table.  ``downgrade`` is therefore a no-op; fresh environments start
empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "plugins",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.String(length=255), nullable=False),
        sa.Column("plugin_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("config_ref", sa.String(length=512), nullable=False),
        sa.Column("health_state", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("allow_listed", sa.Boolean(), nullable=False),
        sa.Column("plugin_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_plugins_plugin_id",
        "plugins",
        ["plugin_id"],
        unique=True,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""
