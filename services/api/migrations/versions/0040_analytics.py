"""analytics_metrics table (0039 -> 0040).

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics_metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("content_ref", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_analytics_metrics_identity",
        "analytics_metrics",
        ["org_id", "content_ref", "kind", "collected_at"],
        unique=True,
    )
    op.create_index(
        "ix_analytics_metrics_org_content",
        "analytics_metrics",
        ["org_id", "content_ref"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_analytics_metrics_org_content", table_name="analytics_metrics")
    op.drop_index("uq_analytics_metrics_identity", table_name="analytics_metrics")
    op.drop_table("analytics_metrics")