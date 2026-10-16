"""bootstrap_manifests, bootstrap_validations, secret_rotations (0070 -> 0071).

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bootstrap_manifests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("manifest_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("secret_refs_json", sa.String(4000), nullable=False),
        sa.Column("tls_cert_ref", sa.String(512), nullable=False),
        sa.Column("tls_key_ref", sa.String(512), nullable=False),
        sa.Column("tls_self_signed", sa.Boolean(), nullable=False),
        sa.Column("domain_ref", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_bootstrap_manifests_org_manifest",
        "bootstrap_manifests",
        ["org_id", "manifest_id"],
        unique=True,
    )
    op.create_index(
        "ix_bootstrap_manifests_org_environment",
        "bootstrap_manifests",
        ["org_id", "environment"],
        unique=False,
    )

    op.create_table(
        "bootstrap_validations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("validation_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("manifest_id", sa.String(128), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("findings_json", sa.String(8000), nullable=False),
        sa.Column("gates_json", sa.String(2000), nullable=False),
        sa.Column("blocking_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_bootstrap_validations_org_validation",
        "bootstrap_validations",
        ["org_id", "validation_id"],
        unique=True,
    )
    op.create_index(
        "ix_bootstrap_validations_org_manifest",
        "bootstrap_validations",
        ["org_id", "manifest_id"],
        unique=False,
    )

    op.create_table(
        "secret_rotations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("connector_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("cadence", sa.String(32), nullable=False),
        sa.Column("cadence_days", sa.Integer(), nullable=False),
        sa.Column("procedure_ref", sa.String(512), nullable=False),
        sa.Column("owner_ref", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_secret_rotations_org_connector",
        "secret_rotations",
        ["org_id", "connector_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_secret_rotations_org_connector", table_name="secret_rotations")
    op.drop_table("secret_rotations")
    op.drop_index("ix_bootstrap_validations_org_manifest", table_name="bootstrap_validations")
    op.drop_index("uq_bootstrap_validations_org_validation", table_name="bootstrap_validations")
    op.drop_table("bootstrap_validations")
    op.drop_index("ix_bootstrap_manifests_org_environment", table_name="bootstrap_manifests")
    op.drop_index("uq_bootstrap_manifests_org_manifest", table_name="bootstrap_manifests")
    op.drop_table("bootstrap_manifests")
