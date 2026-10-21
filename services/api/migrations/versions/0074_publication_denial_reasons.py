"""publication_denial_reasons (0073 -> 0074).

Revision ID: 0074
Revises: 0073
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publication_denial_reasons",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_publication_denial_reasons_job_ordinal",
        "publication_denial_reasons",
        ["job_id", "ordinal"],
        unique=True,
    )
    op.create_index(
        "ix_publication_denial_reasons_org_job",
        "publication_denial_reasons",
        ["org_id", "job_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_publication_denial_reasons_org_job", table_name="publication_denial_reasons")
    op.drop_index(
        "uq_publication_denial_reasons_job_ordinal",
        table_name="publication_denial_reasons",
    )
    op.drop_table("publication_denial_reasons")
