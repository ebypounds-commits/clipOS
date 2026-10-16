"""deployments, release_evidence (0068 -> 0069).

Revision ID: 0069
Revises: 0068
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deployments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("deployment_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("strategy", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("image_digest", sa.String(200), nullable=False),
        sa.Column("migrations_json", sa.String(4000), nullable=False),
        sa.Column("parent_deployment_id", sa.String(128), nullable=True),
        sa.Column("health_json", sa.String(4000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_deployments_org_deployment",
        "deployments",
        ["org_id", "deployment_id"],
        unique=True,
    )
    op.create_index(
        "ix_deployments_org_status",
        "deployments",
        ["org_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_deployments_org_environment",
        "deployments",
        ["org_id", "environment"],
        unique=False,
    )

    op.create_table(
        "release_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("deployment_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("image_digest", sa.String(200), nullable=False),
        sa.Column("migrations_json", sa.String(4000), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.Column("agent_versions_json", sa.String(4000), nullable=False),
        sa.Column("policy_versions_json", sa.String(4000), nullable=False),
        sa.Column("ci_runs_json", sa.String(4000), nullable=False),
        sa.Column("security_scan_summary", sa.String(2000), nullable=False),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_target", sa.String(128), nullable=True),
        sa.Column("operator_approvals_json", sa.String(4000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_release_evidence_org_deployment",
        "release_evidence",
        ["org_id", "deployment_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_release_evidence_org_deployment", table_name="release_evidence")
    op.drop_table("release_evidence")
    op.drop_index("ix_deployments_org_environment", table_name="deployments")
    op.drop_index("ix_deployments_org_status", table_name="deployments")
    op.drop_index("uq_deployments_org_deployment", table_name="deployments")
    op.drop_table("deployments")
