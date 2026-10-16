"""backup_artifacts, restore_tests (0069 -> 0070).

Revision ID: 0070
Revises: 0069
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("key_ref", sa.String(128), nullable=False),
        sa.Column("key_fingerprint", sa.String(64), nullable=False),
        sa.Column("plaintext_sha256", sa.String(64), nullable=False),
        sa.Column("ciphertext_sha256", sa.String(64), nullable=False),
        sa.Column("plaintext_bytes", sa.Integer(), nullable=False),
        sa.Column("ciphertext_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_backup_artifacts_org_artifact",
        "backup_artifacts",
        ["org_id", "artifact_id"],
        unique=True,
    )
    op.create_index(
        "ix_backup_artifacts_org_kind",
        "backup_artifacts",
        ["org_id", "kind"],
        unique=False,
    )

    op.create_table(
        "restore_tests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("test_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(128), nullable=False),
        sa.Column("environment_ref", sa.String(255), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("digest_matched", sa.Boolean(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("rpo_seconds", sa.Float(), nullable=False),
        sa.Column("detail", sa.String(1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_restore_tests_org_test",
        "restore_tests",
        ["org_id", "test_id"],
        unique=True,
    )
    op.create_index(
        "ix_restore_tests_org_artifact",
        "restore_tests",
        ["org_id", "artifact_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_restore_tests_org_artifact", table_name="restore_tests")
    op.drop_index("uq_restore_tests_org_test", table_name="restore_tests")
    op.drop_table("restore_tests")
    op.drop_index("ix_backup_artifacts_org_kind", table_name="backup_artifacts")
    op.drop_index("uq_backup_artifacts_org_artifact", table_name="backup_artifacts")
    op.drop_table("backup_artifacts")
