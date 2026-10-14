"""eval_sandboxes and promotions tables (0060 -> 0061).

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eval_sandboxes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sandbox_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("proposal_id", sa.String(128), nullable=False),
        sa.Column("evaluator_alias", sa.String(128), nullable=False),
        sa.Column("scenario_ref", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_eval_sandboxes_org_sandbox",
        "eval_sandboxes",
        ["org_id", "sandbox_id"],
        unique=True,
    )
    op.create_index(
        "ix_eval_sandboxes_org_proposal",
        "eval_sandboxes",
        ["org_id", "proposal_id"],
        unique=False,
    )
    op.create_table(
        "promotions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("promotion_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("proposal_id", sa.String(128), nullable=False),
        sa.Column("sandbox_id", sa.String(128), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("actor_ref", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_promotions_org_promotion",
        "promotions",
        ["org_id", "promotion_id"],
        unique=True,
    )
    op.create_index(
        "ix_promotions_org_proposal",
        "promotions",
        ["org_id", "proposal_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_promotions_org_proposal", table_name="promotions")
    op.drop_index("uq_promotions_org_promotion", table_name="promotions")
    op.drop_table("promotions")
    op.drop_index("ix_eval_sandboxes_org_proposal", table_name="eval_sandboxes")
    op.drop_index("uq_eval_sandboxes_org_sandbox", table_name="eval_sandboxes")
    op.drop_table("eval_sandboxes")
