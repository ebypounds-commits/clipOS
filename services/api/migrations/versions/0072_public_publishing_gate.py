"""owner_approvals, private_tests (0071 -> 0072).

Revision ID: 0072
Revises: 0071
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "owner_approvals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("approval_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("account_ref", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("owner_ref", sa.String(255), nullable=False),
        sa.Column("private_test_ref", sa.String(255), nullable=False),
        sa.Column("note", sa.String(1000), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_owner_approvals_org_approval",
        "owner_approvals",
        ["org_id", "approval_id"],
        unique=True,
    )
    op.create_index(
        "ix_owner_approvals_org_account",
        "owner_approvals",
        ["org_id", "platform", "account_ref"],
        unique=False,
    )

    op.create_table(
        "private_tests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("test_ref", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("account_ref", sa.String(255), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("detail", sa.String(1000), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_private_tests_org_test",
        "private_tests",
        ["org_id", "test_ref"],
        unique=True,
    )
    op.create_index(
        "ix_private_tests_org_account",
        "private_tests",
        ["org_id", "platform", "account_ref"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_private_tests_org_account", table_name="private_tests")
    op.drop_index("uq_private_tests_org_test", table_name="private_tests")
    op.drop_table("private_tests")
    op.drop_index("ix_owner_approvals_org_account", table_name="owner_approvals")
    op.drop_index("uq_owner_approvals_org_approval", table_name="owner_approvals")
    op.drop_table("owner_approvals")
