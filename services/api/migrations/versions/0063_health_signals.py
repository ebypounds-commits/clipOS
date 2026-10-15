"""health_signals table (0062 -> 0063).

Revision ID: 0063
Revises: 0062
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "health_signals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("signal_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("subsystem", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detail", sa.String(255), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_health_signals_identity",
        "health_signals",
        ["org_id", "signal_id"],
        unique=True,
    )
    op.create_index(
        "ix_health_signals_org_subsystem",
        "health_signals",
        ["org_id", "subsystem"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_health_signals_org_subsystem", table_name="health_signals")
    op.drop_index("uq_health_signals_identity", table_name="health_signals")
    op.drop_table("health_signals")
