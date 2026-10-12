"""Create caption_tracks table (CLIP-052).

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-12

Create-only migration introducing the M12 caption track table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the caption_tracks table."""
    op.create_table(
        "caption_tracks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("render_project_id", sa.String(length=128), nullable=False),
        sa.Column("segments", sa.JSON(), nullable=False),
        sa.Column("style_ref", sa.String(length=128), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_caption_tracks_org_track",
        "caption_tracks",
        ["org_id", "track_id"],
        unique=True,
    )
    op.create_index(
        "ix_caption_tracks_org_render",
        "caption_tracks",
        ["org_id", "render_project_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""
