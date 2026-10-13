"""FastAPI application entrypoint."""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException

import clipos_api.meta_adapter  # registers Meta adapter at import
import clipos_api.tiktok_adapter  # registers TikTok adapter at import
import clipos_api.youtube_adapter  # noqa: F401  # registers YouTube adapter at import
from clipos_api.affiliate_attribution import router as affiliate_attribution_router
from clipos_api.agent_evaluation import router as agent_evaluation_router
from clipos_api.agent_policy import router as agent_policy_router
from clipos_api.agent_registry import router as agent_registry_router
from clipos_api.ai_labels import router as ai_labels_router
from clipos_api.analytics import router as analytics_router
from clipos_api.analytics_dashboards import router as analytics_dashboards_router
from clipos_api.asset_validation import router as asset_validation_router
from clipos_api.budgets import router as budgets_router
from clipos_api.caption_tracks import router as caption_tracks_router
from clipos_api.clip_candidates_router import router as clip_candidates_router
from clipos_api.clip_dedup import router as clip_dedup_router
from clipos_api.clip_review import router as clip_review_router
from clipos_api.clip_scores import router as clip_scores_router
from clipos_api.collection_schedules import router as collection_schedules_router
from clipos_api.content_evals import router as content_evals_router
from clipos_api.content_planning import router as content_planning_router
from clipos_api.cost_ledger import router as cost_ledger_router
from clipos_api.experiment_evaluations import router as experiment_evaluations_router
from clipos_api.experiment_orchestration import router as experiment_orchestration_router
from clipos_api.experiments import router as experiments_router
from clipos_api.generation_adapters import router as generation_adapters_router
from clipos_api.jev_tools import router as jev_tools_router
from clipos_api.llm_cost import CostLedger
from clipos_api.llm_cost import router as llm_cost_router
from clipos_api.llm_gateway import AdapterRegistry, LLMGateway
from clipos_api.local_adapter import build_local_adapter, local_adapter_provider_name
from clipos_api.mcp_boundary import McpBoundary, build_mcp_boundary
from clipos_api.media_ingestion import router as media_ingestion_router
from clipos_api.metric_reconciliation import router as metric_reconciliation_router
from clipos_api.model_registry import ModelRegistryStore
from clipos_api.model_registry import router as model_registry_router
from clipos_api.operator import OperatorSnapshot
from clipos_api.opportunities import router as opportunity_router
from clipos_api.performance_snapshots import router as performance_snapshots_router
from clipos_api.platform_accounts import router as platform_accounts_router
from clipos_api.plugin_registry import router as plugin_registry_router
from clipos_api.profitability import router as profitability_router
from clipos_api.provenance import router as provenance_router
from clipos_api.publishing import router as publishing_router
from clipos_api.render_pipeline import router as render_pipeline_router
from clipos_api.render_project import router as render_project_router
from clipos_api.render_variants import router as render_variants_router
from clipos_api.repository import get_engine
from clipos_api.research import router as research_router
from clipos_api.revenue_ledger import router as revenue_ledger_router
from clipos_api.revenue_reconciliation import router as revenue_reconciliation_router
from clipos_api.rights_entities import router as rights_entities_router
from clipos_api.rights_gate import router as rights_gate_router
from clipos_api.rights_ledger import router as rights_ledger_router
from clipos_api.script_hooks import router as script_hooks_router
from clipos_api.semantic import router as semantic_router
from clipos_api.signal_sources import router as signal_sources_router
from clipos_api.sponsorship import router as sponsorship_router
from clipos_api.stock_adapter import router as stock_adapter_router
from clipos_api.strategy_rules import router as strategy_rules_router
from clipos_api.tool_policy import (
    SandboxPolicy,
    ToolPolicyEngine,
    ToolPolicyRequest,
    build_tool_policy,
)
from clipos_api.tool_registry import router as tool_registry_router
from clipos_api.topics import router as topics_router
from clipos_api.transcription import router as transcription_router
from clipos_api.voice_assets import router as voice_assets_router
from clipos_api.watchlists import router as watchlist_router

try:
    APP_VERSION = version("clipos-api")
except PackageNotFoundError:
    APP_VERSION = "0.0.0"

app = FastAPI(title="ClipOS API", version=APP_VERSION)
app.router.routes.extend(agent_registry_router.routes)
app.router.routes.extend(affiliate_attribution_router.routes)
app.router.routes.extend(ai_labels_router.routes)
app.router.routes.extend(analytics_router.routes)
app.router.routes.extend(analytics_dashboards_router.routes)
app.router.routes.extend(agent_policy_router.routes)
app.router.routes.extend(agent_evaluation_router.routes)
app.router.routes.extend(model_registry_router.routes)
app.router.routes.extend(llm_cost_router.routes)
app.router.routes.extend(tool_registry_router.routes)
app.router.routes.extend(plugin_registry_router.routes)
app.router.routes.extend(profitability_router.routes)
app.router.routes.extend(platform_accounts_router.routes)
app.router.routes.extend(rights_entities_router.routes)
app.router.routes.extend(rights_ledger_router.routes)
app.router.routes.extend(rights_gate_router.routes)
app.router.routes.extend(signal_sources_router.routes)
app.router.routes.extend(stock_adapter_router.routes)
app.router.routes.extend(sponsorship_router.routes)
app.router.routes.extend(strategy_rules_router.routes)
app.router.routes.extend(transcription_router.routes)
app.router.routes.extend(voice_assets_router.routes)
app.router.routes.extend(semantic_router.routes)
app.router.routes.extend(topics_router.routes)
app.router.routes.extend(opportunity_router.routes)
app.router.routes.extend(performance_snapshots_router.routes)
app.router.routes.extend(provenance_router.routes)
app.router.routes.extend(publishing_router.routes)
app.router.routes.extend(render_pipeline_router.routes)
app.router.routes.extend(render_project_router.routes)
app.router.routes.extend(render_variants_router.routes)
app.router.routes.extend(research_router.routes)
app.router.routes.extend(revenue_ledger_router.routes)
app.router.routes.extend(revenue_reconciliation_router.routes)
app.router.routes.extend(media_ingestion_router.routes)
app.router.routes.extend(metric_reconciliation_router.routes)
app.router.routes.extend(watchlist_router.routes)
app.router.routes.extend(asset_validation_router.routes)
app.router.routes.extend(budgets_router.routes)
app.router.routes.extend(caption_tracks_router.routes)
app.router.routes.extend(clip_candidates_router.routes)
app.router.routes.extend(clip_dedup_router.routes)
app.router.routes.extend(clip_review_router.routes)
app.router.routes.extend(clip_scores_router.routes)
app.router.routes.extend(collection_schedules_router.routes)
app.router.routes.extend(content_evals_router.routes)
app.router.routes.extend(content_planning_router.routes)
app.router.routes.extend(experiments_router.routes)
app.router.routes.extend(cost_ledger_router.routes)
app.router.routes.extend(experiment_evaluations_router.routes)
app.router.routes.extend(experiment_orchestration_router.routes)
app.router.routes.extend(generation_adapters_router.routes)
app.router.routes.extend(jev_tools_router.routes)
app.router.routes.extend(script_hooks_router.routes)


@app.get("/health")
def health() -> dict[str, str]:
    """Return a machine-readable process health payload."""
    return {"status": "ok", "version": APP_VERSION}


def database_engine() -> sa.Engine:
    """Return the process-cached engine configured by ``DATABASE_URL``."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise HTTPException(status_code=503, detail="database not configured")
    try:
        return get_engine(url)
    except sa.exc.ArgumentError as exc:
        raise HTTPException(status_code=503, detail="database configuration invalid") from exc


@app.get("/operator/health")
def operator_health(engine: Annotated[sa.Engine, Depends(database_engine)]) -> OperatorSnapshot:
    """Return the read-only operator snapshot; Temporal reachability is caller-supplied."""
    return OperatorSnapshot.build(
        engine,
        datetime.now(UTC),
        temporal_reachable=bool(os.environ.get("TEMPORAL_ADDRESS")),
    )


def build_llm_gateway(engine: sa.Engine) -> LLMGateway:
    """Production wiring point: operators register adapter factories here."""
    gateway = LLMGateway(
        ModelRegistryStore.from_engine(engine),
        AdapterRegistry(),
        ledger=CostLedger.from_engine(engine),
    )
    if os.environ.get("LOCAL_ADAPTER_BASE_URL"):
        gateway.register_adapter(local_adapter_provider_name(), build_local_adapter)
    return gateway


def build_mcp_client_boundary(engine: sa.Engine) -> McpBoundary:
    """Production wiring point for the MCP tools/call boundary."""
    return build_mcp_boundary(engine)


def build_tool_policy_engine(
    engine: sa.Engine,
    sandbox_for: Callable[[ToolPolicyRequest], SandboxPolicy],
) -> ToolPolicyEngine:
    """Production wiring point composing tool capability checks with policy."""
    return build_tool_policy(engine, sandbox_for)
