"""FastAPI application entrypoint."""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException

from clipos_api.agent_evaluation import router as agent_evaluation_router
from clipos_api.agent_policy import router as agent_policy_router
from clipos_api.agent_registry import router as agent_registry_router
from clipos_api.llm_cost import CostLedger
from clipos_api.llm_cost import router as llm_cost_router
from clipos_api.llm_gateway import AdapterRegistry, LLMGateway
from clipos_api.local_adapter import build_local_adapter, local_adapter_provider_name
from clipos_api.mcp_boundary import McpBoundary, build_mcp_boundary
from clipos_api.model_registry import ModelRegistryStore
from clipos_api.model_registry import router as model_registry_router
from clipos_api.operator import OperatorSnapshot
from clipos_api.plugin_registry import router as plugin_registry_router
from clipos_api.provenance import router as provenance_router
from clipos_api.repository import get_engine
from clipos_api.rights_entities import router as rights_entities_router
from clipos_api.rights_ledger import router as rights_ledger_router
from clipos_api.tool_policy import (
    SandboxPolicy,
    ToolPolicyEngine,
    ToolPolicyRequest,
    build_tool_policy,
)
from clipos_api.tool_registry import router as tool_registry_router

try:
    APP_VERSION = version("clipos-api")
except PackageNotFoundError:
    APP_VERSION = "0.0.0"

app = FastAPI(title="ClipOS API", version=APP_VERSION)
app.router.routes.extend(agent_registry_router.routes)
app.router.routes.extend(agent_policy_router.routes)
app.router.routes.extend(agent_evaluation_router.routes)
app.router.routes.extend(model_registry_router.routes)
app.router.routes.extend(llm_cost_router.routes)
app.router.routes.extend(tool_registry_router.routes)
app.router.routes.extend(plugin_registry_router.routes)
app.router.routes.extend(rights_entities_router.routes)
app.router.routes.extend(rights_ledger_router.routes)
app.router.routes.extend(provenance_router.routes)


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
