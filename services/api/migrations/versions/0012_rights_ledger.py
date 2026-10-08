"""Rights ledger: CREATE-only tables.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-08

CREATE-only by policy: this migration introduces only the fresh rights ledger
tables.  ``downgrade`` is therefore a no-op; fresh environments start empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the five rights-ledger tables."""
    op.execute(
        sa.text(
            """
CREATE TABLE rights_records (
    id INTEGER NOT NULL,
    record_id VARCHAR(128) NOT NULL,
    item_id VARCHAR(128) NOT NULL,
    org_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    basis VARCHAR(32) NOT NULL,
    reviewed_by VARCHAR(255) NOT NULL,
    record_metadata JSON NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_rights_records_record_id UNIQUE (record_id),
    CONSTRAINT uq_rights_records_item_org UNIQUE (item_id, org_id)
);
CREATE INDEX ix_rights_records_org_created ON rights_records (org_id, created_at);
CREATE INDEX ix_rights_records_item_status ON rights_records (item_id, status);

CREATE TABLE licenses (
    id INTEGER NOT NULL,
    license_id VARCHAR(128) NOT NULL,
    record_id VARCHAR(128) NOT NULL,
    org_id VARCHAR(64) NOT NULL,
    licensor_ref VARCHAR(512) NOT NULL,
    licensee_ref VARCHAR(512) NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ,
    terms_ref VARCHAR(512) NOT NULL,
    license_metadata JSON NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_licenses_license_id UNIQUE (license_id),
    CONSTRAINT uq_licenses_record_terms_start UNIQUE (record_id, terms_ref, starts_at)
);
CREATE INDEX ix_licenses_org_created ON licenses (org_id, created_at);
CREATE INDEX ix_licenses_record ON licenses (record_id);

CREATE TABLE license_evidence (
    id INTEGER NOT NULL,
    evidence_id VARCHAR(128) NOT NULL,
    license_id VARCHAR(128) NOT NULL,
    org_id VARCHAR(64) NOT NULL,
    evidence_type VARCHAR(32) NOT NULL,
    evidence_ref VARCHAR(512) NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_license_evidence_evidence_id UNIQUE (evidence_id)
);
CREATE INDEX ix_license_evidence_org_captured ON license_evidence (org_id, captured_at);
CREATE INDEX ix_license_evidence_license ON license_evidence (license_id);

CREATE TABLE attribution_requirements (
    id INTEGER NOT NULL,
    requirement_id VARCHAR(128) NOT NULL,
    record_id VARCHAR(128) NOT NULL,
    org_id VARCHAR(64) NOT NULL,
    requirement VARCHAR(1024) NOT NULL,
    applies_to VARCHAR(32) NOT NULL,
    requirement_metadata JSON NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_attribution_requirements_id UNIQUE (requirement_id),
    CONSTRAINT uq_attribution_requirements_record_scope UNIQUE (record_id, applies_to, requirement)
);
CREATE INDEX ix_attribution_requirements_org_created ON attribution_requirements (org_id, created_at);
CREATE INDEX ix_attribution_requirements_record ON attribution_requirements (record_id);

CREATE TABLE rights_events (
    id INTEGER NOT NULL,
    event_id VARCHAR(128) NOT NULL,
    record_id VARCHAR(128) NOT NULL,
    org_id VARCHAR(64) NOT NULL,
    from_status VARCHAR(32) NOT NULL,
    to_status VARCHAR(32) NOT NULL,
    reason_ref VARCHAR(512),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_rights_events_event_id UNIQUE (event_id)
);
CREATE INDEX ix_rights_events_org_created ON rights_events (org_id, created_at);
CREATE INDEX ix_rights_events_record ON rights_events (record_id);
"""
        )
    )


def downgrade() -> None:
    """Intentionally not implemented: this migration is CREATE-only."""
