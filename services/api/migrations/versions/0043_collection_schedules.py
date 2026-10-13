"""collection_schedules table (0042 -> 0043).

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_schedules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("schedule_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("account_ref", sa.String(255), nullable=False),
        sa.Column("content_ref", sa.String(512), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("schedule_id"),
    )
    op.create_index(
        "uq_collection_schedules_org_content",
        "collection_schedules",
        ["org_id", "content_ref"],
        unique=True,
    )
    op.create_index(
        "ix_collection_schedules_org_enabled",
        "collection_schedules",
        ["org_id", "enabled"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_collection_schedules_org_enabled", table_name="collection_schedules")
    op.drop_index("uq_collection_schedules_org_content", table_name="collection_schedules")
    op.drop_table("collection_schedules")
