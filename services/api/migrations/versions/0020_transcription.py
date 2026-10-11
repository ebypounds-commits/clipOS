"""Transcription tables (CLIP-040).

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-11

CREATE-only migration: introduces transcripts and transcript_segments tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the transcripts and transcript_segments tables."""
    op.create_table(
        "transcripts",
        sa.Column("transcript_id", sa.String(length=36), nullable=False),
        sa.Column("media_asset_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("provider_alias", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("model_hint", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("transcript_id"),
    )
    op.create_index(
        "ix_transcripts_org_media_lang",
        "transcripts",
        ["org_id", "media_asset_id", "language"],
        unique=True,
    )
    op.create_index(
        "ix_transcripts_status",
        "transcripts",
        ["status"],
        unique=False,
    )

    op.create_table(
        "transcript_segments",
        sa.Column("segment_id", sa.String(length=36), nullable=False),
        sa.Column("transcript_id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(length=2048), nullable=False),
        sa.Column("speaker", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("segment_id"),
    )
    op.create_index(
        "ix_transcript_segments_transcript_seq",
        "transcript_segments",
        ["transcript_id", "seq"],
        unique=True,
    )
    op.create_index(
        "ix_transcript_segments_transcript_id",
        "transcript_segments",
        ["transcript_id"],
        unique=False,
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""
