"""experiment_evaluations table (0045 -> 0046).

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "experiment_evaluations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("eval_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("experiment_id", sa.String(128), nullable=False),
        sa.Column("variant_a_key", sa.String(64), nullable=False),
        sa.Column("variant_a_sample", sa.Integer(), nullable=False),
        sa.Column("variant_a_mean", sa.Float(), nullable=False),
        sa.Column("variant_a_stddev", sa.Float(), nullable=False),
        sa.Column("variant_b_key", sa.String(64), nullable=False),
        sa.Column("variant_b_sample", sa.Integer(), nullable=False),
        sa.Column("variant_b_mean", sa.Float(), nullable=False),
        sa.Column("variant_b_stddev", sa.Float(), nullable=False),
        sa.Column("z_critical", sa.Float(), nullable=False),
        sa.Column("min_effect_pct", sa.Float(), nullable=False),
        sa.Column("lift_pct", sa.Float(), nullable=False),
        sa.Column("z_score", sa.Float(), nullable=False),
        sa.Column("p_value", sa.Float(), nullable=False),
        sa.Column("statistically_significant", sa.Boolean(), nullable=False),
        sa.Column("business_significant", sa.Boolean(), nullable=False),
        sa.Column("winner_variant_key", sa.String(64), nullable=True),
        sa.Column("actor_ref", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_experiment_evaluations_identity",
        "experiment_evaluations",
        ["org_id", "eval_id"],
        unique=True,
    )
    op.create_index(
        "ix_experiment_evaluations_org_experiment",
        "experiment_evaluations",
        ["org_id", "experiment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_experiment_evaluations_org_experiment",
        table_name="experiment_evaluations",
    )
    op.drop_index(
        "uq_experiment_evaluations_identity",
        table_name="experiment_evaluations",
    )
    op.drop_table("experiment_evaluations")
