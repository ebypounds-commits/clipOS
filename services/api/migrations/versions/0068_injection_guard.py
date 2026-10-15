"""injection_events table (0067 -> 0068).

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "injection_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("source_ref", sa.String(512), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("highest_severity", sa.String(16), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("content_chars", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_injection_events_identity",
        "injection_events",
        ["org_id", "event_id"],
        unique=True,
    )
    op.create_index(
        "ix_injection_events_org_decision",
        "injection_events",
        ["org_id", "decision"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_injection_events_org_decision", table_name="injection_events")
    op.drop_index("uq_injection_events_identity", table_name="injection_events")
    op.drop_table("injection_events")
