"""Signal sources and observations tables: CREATE-only.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-08

CREATE-only by policy: this migration introduces only the fresh signal tables.
``downgrade`` is therefore a no-op; fresh environments start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the two signal tables."""
    op.create_table(
        "signal_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("canonical_url", sa.String(length=512), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_signal_sources_org_source",
        "signal_sources",
        ["org_id", "source_id"],
        unique=True,
    )
    op.create_index(
        "ix_signal_sources_org_created",
        "signal_sources",
        ["org_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "signal_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("observation_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("payload_ref", sa.String(length=512), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("observation_id", name="uq_signal_observations_observation_id"),
    )
    op.create_index(
        "ix_signal_observations_org_created",
        "signal_observations",
        ["org_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""
