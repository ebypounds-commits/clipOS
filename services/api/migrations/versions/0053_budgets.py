"""budgets and spend_authorizations tables (0052 -> 0053).

Revision ID: 0053
Revises: 0052
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("budget_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_budgets_org_budget",
        "budgets",
        ["org_id", "budget_id"],
        unique=True,
    )
    op.create_index(
        "ix_budgets_org_scope",
        "budgets",
        ["org_id", "scope_key"],
        unique=False,
    )
    op.create_table(
        "spend_authorizations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("authorization_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("budget_id", sa.String(128), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("cap_cents", sa.BigInteger(), nullable=False),
        sa.Column("spent_before_cents", sa.BigInteger(), nullable=False),
        sa.Column("requested_cents", sa.BigInteger(), nullable=False),
        sa.Column("remaining_before", sa.BigInteger(), nullable=False),
        sa.Column("remaining_after", sa.BigInteger(), nullable=True),
        sa.Column("actor_ref", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_spend_authorizations_identity",
        "spend_authorizations",
        ["org_id", "authorization_id"],
        unique=True,
    )
    op.create_index(
        "ix_spend_authorizations_org_budget",
        "spend_authorizations",
        ["org_id", "budget_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_spend_authorizations_org_budget",
        table_name="spend_authorizations",
    )
    op.drop_index(
        "uq_spend_authorizations_identity",
        table_name="spend_authorizations",
    )
    op.drop_table("spend_authorizations")
    op.drop_index("ix_budgets_org_scope", table_name="budgets")
    op.drop_index("uq_budgets_org_budget", table_name="budgets")
    op.drop_table("budgets")
