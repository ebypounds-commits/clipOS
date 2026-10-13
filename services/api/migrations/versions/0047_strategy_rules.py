"""strategy_rules table (0046 -> 0047).

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rule_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("rule_kind", sa.String(64), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_strategy_rules_org_rule",
        "strategy_rules",
        ["org_id", "rule_id"],
        unique=True,
    )
    op.create_index(
        "ix_strategy_rules_org_status",
        "strategy_rules",
        ["org_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_rules_org_status", table_name="strategy_rules")
    op.drop_index("uq_strategy_rules_org_rule", table_name="strategy_rules")
    op.drop_table("strategy_rules")
