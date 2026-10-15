"""threats, threat_controls, threat_mitigations, abuse_cases (0066 -> 0067).

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "threats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("threat_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("abuse_case", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("controls", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_threats_org_threat", "threats", ["org_id", "threat_id"], unique=True)
    op.create_index("ix_threats_org_category", "threats", ["org_id", "category"], unique=False)
    op.create_table(
        "threat_controls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("threat_id", sa.String(128), nullable=False),
        sa.Column("control_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_threat_controls_identity",
        "threat_controls",
        ["org_id", "threat_id", "control_id"],
        unique=True,
    )
    op.create_table(
        "threat_mitigations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("mitigation_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("threat_id", sa.String(128), nullable=False),
        sa.Column("control_ref", sa.String(512), nullable=False),
        sa.Column("evidence_ref", sa.String(512), nullable=False),
        sa.Column("note", sa.String(1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_threat_mitigations_identity",
        "threat_mitigations",
        ["org_id", "threat_id", "mitigation_id"],
        unique=True,
    )
    op.create_table(
        "abuse_cases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("abuse_case_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("attack_surface", sa.String(512), nullable=False),
        sa.Column("scenario", sa.String(2000), nullable=False),
        sa.Column("detection_ref", sa.String(512), nullable=True),
        sa.Column("threat_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_abuse_cases_org_case",
        "abuse_cases",
        ["org_id", "abuse_case_id"],
        unique=True,
    )
    op.create_index(
        "ix_abuse_cases_org_surface",
        "abuse_cases",
        ["org_id", "attack_surface"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_abuse_cases_org_surface", table_name="abuse_cases")
    op.drop_index("uq_abuse_cases_org_case", table_name="abuse_cases")
    op.drop_table("abuse_cases")
    op.drop_index("uq_threat_mitigations_identity", table_name="threat_mitigations")
    op.drop_table("threat_mitigations")
    op.drop_index("uq_threat_controls_identity", table_name="threat_controls")
    op.drop_table("threat_controls")
    op.drop_index("ix_threats_org_category", table_name="threats")
    op.drop_index("uq_threats_org_threat", table_name="threats")
    op.drop_table("threats")
