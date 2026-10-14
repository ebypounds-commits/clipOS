"""improvement_proposals and proposal_evidence tables (0059 -> 0060).

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "improvement_proposals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("proposal_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("weakness_ref", sa.String(512), nullable=False),
        sa.Column("summary", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_improvement_proposals_org_proposal",
        "improvement_proposals",
        ["org_id", "proposal_id"],
        unique=True,
    )
    op.create_index(
        "ix_improvement_proposals_org_status",
        "improvement_proposals",
        ["org_id", "status"],
        unique=False,
    )
    op.create_table(
        "proposal_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("evidence_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("proposal_id", sa.String(128), nullable=False),
        sa.Column("evidence_ref", sa.String(512), nullable=False),
        sa.Column("evidence_kind", sa.String(64), nullable=False),
        sa.Column("note_ref", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_proposal_evidence_org_evidence",
        "proposal_evidence",
        ["org_id", "evidence_id"],
        unique=True,
    )
    op.create_index(
        "ix_proposal_evidence_org_proposal",
        "proposal_evidence",
        ["org_id", "proposal_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_proposal_evidence_org_proposal",
        table_name="proposal_evidence",
    )
    op.drop_index(
        "uq_proposal_evidence_org_evidence",
        table_name="proposal_evidence",
    )
    op.drop_table("proposal_evidence")
    op.drop_index(
        "ix_improvement_proposals_org_status",
        table_name="improvement_proposals",
    )
    op.drop_index(
        "uq_improvement_proposals_org_proposal",
        table_name="improvement_proposals",
    )
    op.drop_table("improvement_proposals")
