"""monitor_checks table (0065 -> 0066).

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monitor_checks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("check_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("subsystem", sa.String(64), nullable=False),
        sa.Column("monitor_kind", sa.String(32), nullable=False),
        sa.Column("subject_ref", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("measured", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("detail", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_monitor_checks_identity",
        "monitor_checks",
        ["org_id", "check_id"],
        unique=True,
    )
    op.create_index(
        "ix_monitor_checks_org_kind",
        "monitor_checks",
        ["org_id", "monitor_kind"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_monitor_checks_org_kind", table_name="monitor_checks")
    op.drop_index("uq_monitor_checks_identity", table_name="monitor_checks")
    op.drop_table("monitor_checks")
