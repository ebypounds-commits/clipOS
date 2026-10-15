"""engineering_tickets table (0061 -> 0062).

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "engineering_tickets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("proposal_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("orchestrator_run_id", sa.String(128), nullable=False),
        sa.Column("branch", sa.String(255), nullable=False),
        sa.Column("worktree", sa.String(512), nullable=False),
        sa.Column("lease_expires_at", sa.String(64), nullable=False),
        sa.Column("packet_markdown", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_engineering_tickets_org_ticket",
        "engineering_tickets",
        ["org_id", "ticket_id"],
        unique=True,
    )
    op.create_index(
        "ix_engineering_tickets_org_status",
        "engineering_tickets",
        ["org_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_engineering_tickets_org_status", table_name="engineering_tickets")
    op.drop_index("uq_engineering_tickets_org_ticket", table_name="engineering_tickets")
    op.drop_table("engineering_tickets")
