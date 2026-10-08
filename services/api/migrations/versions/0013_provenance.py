"""Provenance manifests and content fingerprints: CREATE-only tables.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-08

CREATE-only by policy: this migration introduces only the fresh provenance
tables.  ``downgrade`` is therefore a no-op; fresh environments start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the two provenance tables."""
    op.execute(
        sa.text(
            """
CREATE TABLE provenance_manifests (
    id INTEGER NOT NULL,
    manifest_id VARCHAR(128) NOT NULL,
    item_id VARCHAR(128) NOT NULL,
    org_id VARCHAR(64) NOT NULL,
    chain JSON NOT NULL,
    manifest_hash VARCHAR(64),
    generated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_provenance_manifests_manifest_id UNIQUE (manifest_id),
    CONSTRAINT uq_provenance_manifests_item_org UNIQUE (item_id, org_id)
);
CREATE INDEX ix_provenance_manifests_org_generated ON provenance_manifests (org_id, generated_at);
CREATE INDEX ix_provenance_manifests_item ON provenance_manifests (item_id);

CREATE TABLE content_fingerprints (
    id INTEGER NOT NULL,
    fingerprint_id VARCHAR(128) NOT NULL,
    manifest_id VARCHAR(128) NOT NULL,
    org_id VARCHAR(64) NOT NULL,
    algorithm VARCHAR(16) NOT NULL,
    fingerprint_hex VARCHAR(64) NOT NULL,
    artifact_ref VARCHAR(512) NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_content_fingerprints_fingerprint_id UNIQUE (fingerprint_id),
    CONSTRAINT uq_content_fingerprints_manifest_algorithm UNIQUE (manifest_id, algorithm)
);
CREATE INDEX ix_content_fingerprints_org_created ON content_fingerprints (org_id, created_at);
CREATE INDEX ix_content_fingerprints_manifest ON content_fingerprints (manifest_id);
"""
        )
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""
