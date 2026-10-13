"""experiment_variants and experiment_assignments tables (0044 -> 0045).

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "experiment_variants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("experiment_id", sa.String(128), nullable=False),
        sa.Column("variant_key", sa.String(64), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_experiment_variants_identity",
        "experiment_variants",
        ["org_id", "experiment_id", "variant_key"],
        unique=True,
    )
    op.create_index(
        "ix_experiment_variants_org_experiment",
        "experiment_variants",
        ["org_id", "experiment_id"],
        unique=False,
    )
    op.create_table(
        "experiment_assignments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("assignment_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("experiment_id", sa.String(128), nullable=False),
        sa.Column("subject_ref", sa.String(255), nullable=False),
        sa.Column("variant_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assignment_id"),
    )
    op.create_index(
        "uq_experiment_assignments_identity",
        "experiment_assignments",
        ["org_id", "experiment_id", "subject_ref"],
        unique=True,
    )
    op.create_index(
        "ix_experiment_assignments_org_variant",
        "experiment_assignments",
        ["org_id", "experiment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_experiment_assignments_org_variant",
        table_name="experiment_assignments",
    )
    op.drop_index(
        "uq_experiment_assignments_identity",
        table_name="experiment_assignments",
    )
    op.drop_table("experiment_assignments")
    op.drop_index(
        "ix_experiment_variants_org_experiment",
        table_name="experiment_variants",
    )
    op.drop_index(
        "uq_experiment_variants_identity",
        table_name="experiment_variants",
    )
    op.drop_table("experiment_variants")
