"""runbooks, runbook_steps, incidents, incident_timeline (0063 -> 0064).

Revision ID: 0064
Revises: 0063
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runbooks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("runbook_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_runbooks_org_runbook",
        "runbooks",
        ["org_id", "runbook_id"],
        unique=True,
    )
    op.create_index(
        "ix_runbooks_org_category",
        "runbooks",
        ["org_id", "category"],
        unique=False,
    )
    op.create_table(
        "runbook_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("runbook_id", sa.String(128), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("instruction", sa.String(2000), nullable=False),
        sa.Column("command_ref", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_runbook_steps_identity",
        "runbook_steps",
        ["org_id", "runbook_id", "step_no"],
        unique=True,
    )
    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("subsystem", sa.String(64), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("runbook_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_incidents_org_incident",
        "incidents",
        ["org_id", "incident_id"],
        unique=True,
    )
    op.create_index(
        "ix_incidents_org_status",
        "incidents",
        ["org_id", "status"],
        unique=False,
    )
    op.create_table(
        "incident_timeline",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entry_id", sa.String(128), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("note_ref", sa.String(512), nullable=True),
        sa.Column("actor_ref", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_incident_timeline_identity",
        "incident_timeline",
        ["org_id", "entry_id"],
        unique=True,
    )
    op.create_index(
        "ix_incident_timeline_org_incident",
        "incident_timeline",
        ["org_id", "incident_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_incident_timeline_org_incident", table_name="incident_timeline")
    op.drop_index("uq_incident_timeline_identity", table_name="incident_timeline")
    op.drop_table("incident_timeline")
    op.drop_index("ix_incidents_org_status", table_name="incidents")
    op.drop_index("uq_incidents_org_incident", table_name="incidents")
    op.drop_table("incidents")
    op.drop_index("uq_runbook_steps_identity", table_name="runbook_steps")
    op.drop_table("runbook_steps")
    op.drop_index("ix_runbooks_org_category", table_name="runbooks")
    op.drop_index("uq_runbooks_org_runbook", table_name="runbooks")
    op.drop_table("runbooks")
