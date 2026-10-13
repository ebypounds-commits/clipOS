"""performance_snapshots table (0041 -> 0042).

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "performance_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("content_ref", sa.String(512), nullable=False),
        sa.Column("views", sa.Float(), nullable=False),
        sa.Column("likes", sa.Float(), nullable=False),
        sa.Column("comments", sa.Float(), nullable=False),
        sa.Column("shares", sa.Float(), nullable=False),
        sa.Column("impressions", sa.Float(), nullable=False),
        sa.Column("watch_time_minutes", sa.Float(), nullable=False),
        sa.Column("click_throughs", sa.Float(), nullable=False),
        sa.Column("engagement_rate", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id"),
    )
    op.create_index(
        "uq_performance_snapshots_org_content",
        "performance_snapshots",
        ["org_id", "content_ref"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_performance_snapshots_org_content", table_name="performance_snapshots")
    op.drop_table("performance_snapshots")
