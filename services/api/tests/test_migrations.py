"""Migration tests: offline SQL rendering always runs; PG tests need DATABASE_URL."""

from __future__ import annotations

import contextlib
import io
import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

_API_DIR = Path(__file__).resolve().parents[1]
_INI = _API_DIR / "alembic.ini"
_EXPECTED_TABLES = {
    "users",
    "organizations",
    "memberships",
    "sessions",
    "service_identities",
    "audit_events",
    "outbox_events",
    "idempotency_records",
    "agent_records",
    "agent_versions",
    "model_alias_targets",
    "agent_policy_overrides",
    "model_routes",
    "agent_budget_configs",
    "llm_dispatch_records",
    "tools",
    "tool_capability_grants",
    "media_assets",
    "asset_versions",
    "transcripts",
    "transcript_segments",
    "semantic_segments",
    "segment_embeddings",
    "clip_candidates",
    "clip_scores",
    "candidate_fingerprints",
    "candidate_decisions",
    "research_packets",
    "claims",
    "claim_sources",
    "content_briefs",
    "content_plans",
    "hooks",
    "scripts",
    "script_versions",
    "factuality_evaluations",
    "transformation_records",
    "render_projects",
    "render_jobs",
    "caption_tracks",
    "render_outputs",
    "qc_reports",
    "voice_assets",
    "image_assets",
    "source_items",
    "ai_labels",
    "publish_policies",
    "publication_jobs",
    "platform_accounts",
    "analytics_metrics",
    "reconciliation_records",
    "performance_snapshots",
    "collection_schedules",
    "experiments",
    "hypotheses",
    "experiment_variants",
    "experiment_assignments",
    "experiment_evaluations",
    "strategy_rules",
    "revenue_sources",
    "revenue_entries",
    "affiliate_links",
    "affiliate_clicks",
    "affiliate_conversions",
    "sponsor_campaigns",
    "sponsor_deliverables",
    "sponsor_attributions",
    "revenue_reconciliations",
}


def alembic_config() -> Config:
    cfg = Config(str(_INI))
    cfg.set_main_option("script_location", str(_API_DIR / "migrations"))
    cfg.set_main_option("prepend_sys_path", str(_API_DIR))
    return cfg


def test_offline_sql_renders_create_only_schema() -> None:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
        command.upgrade(alembic_config(), "head", sql=True)
    sql = buffer.getvalue()
    for table in _EXPECTED_TABLES:
        assert re.search(rf"CREATE TABLE {table}\b", sql, re.IGNORECASE), f"missing {table}"
    assert "CREATE UNIQUE INDEX ix_organizations_slug" in sql
    assert "CREATE UNIQUE INDEX uq_memberships_org_user" in sql
    assert "CREATE UNIQUE INDEX ix_idempotency_records_business_key" in sql


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; PostgreSQL migrations are exercised by CI lane 5",
)
def test_postgres_upgrade_head_is_idempotent() -> None:
    cfg = alembic_config()
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # second run must be a no-op
    engine_url = cfg.get_main_option("sqlalchemy.url")
    assert engine_url is not None
    from sqlalchemy import create_engine

    inspector = inspect(create_engine(engine_url))
    tables = set(inspector.get_table_names())
    assert _EXPECTED_TABLES <= tables
