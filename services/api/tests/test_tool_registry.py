"""Tests for the tool registry and agent-version capability grants (CLIP-026)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit, tool_registry
from clipos_api.agent_registry import (
    AgentClass,
    AgentRegistryStore,
    AgentState,
    AgentVersion,
    RequiredCapabilities,
    RiskClassification,
    SpendCeilings,
)
from clipos_api.main import app
from clipos_api.tool_registry import (
    CapabilityGrantRequest,
    ToolDef,
    ToolRegistryStore,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tool_registry.Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-026-tool-registry-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ToolRegistryStore:
    return ToolRegistryStore.from_engine(engine)


@pytest.fixture()
def agents(engine: Engine, audit_log: Path) -> AgentRegistryStore:
    return AgentRegistryStore.from_engine(engine)


def _tool(tool_id: str = "web-search", **changes: object) -> ToolDef:
    values = {
        "tool_id": tool_id,
        "name": "Web Search",
        "capability_scopes": ["network.search"],
        "budget_per_run": 0.25,
        "enabled": True,
        "metadata": {"timeout_seconds": 5},
    }
    values.update(changes)
    return ToolDef.model_validate(values)


def _definition(tool_grants: list[str], prohibited: list[str]) -> AgentVersion:
    return AgentVersion(
        name="runner",
        purpose="exercise capability checks",
        agent_class=AgentClass.OPERATIONS,
        prompt_version_ref="prompt-v1",
        model_alias="reasoning-standard",
        required_capabilities=RequiredCapabilities(),
        tool_grants=tool_grants,
        prohibited_tools=prohibited,
        memory_policy={},
        max_iterations=1,
        max_tool_calls=1,
        timeout_seconds=1.0,
        spend_ceilings=SpendCeilings(per_run=0, hourly=0, daily=0, monthly=0),
        concurrency_ceiling=1,
        evaluators=[{"name": "basic", "required_score": 1.0}],
        retry_policy={},
        escalation_policy={},
        risk_classification=RiskClassification.LOW,
    )


def _agent(
    agents_store: AgentRegistryStore,
    agent_id: str = "ops-agent",
    version: str = "v1",
    **definition_changes: object,
) -> str:
    values = {"tool_grants": [], "prohibited_tools": []}
    values.update(definition_changes)
    agents_store.register_version(
        agent_id, version, _definition(values["tool_grants"], values["prohibited_tools"])
    )
    return agent_id


def test_register_is_idempotent_and_rejects_conflicts(store: ToolRegistryStore) -> None:
    registered = store.register(_tool())
    assert registered.enabled is True
    assert store.register(_tool()) == registered
    with pytest.raises(ValueError, match="already registered"):
        store.register(_tool(budget_per_run=1.0))
    with pytest.raises(ValueError, match="tool already registered"):
        store.register(_tool(capability_scopes=["network.other"]))


def test_grant_revoke_lifecycle_is_audited(
    store: ToolRegistryStore, agents: AgentRegistryStore, audit_log: Path
) -> None:
    store.register(_tool())
    _agent(agents)
    granted = store.grant("web-search", "ops-agent", "v1")
    assert granted.active is True
    assert store.grant("web-search", "ops-agent", "v1") == granted
    assert store.list_grants("web-search") == [granted]

    revoked = store.revoke("web-search", "ops-agent", "v1")
    assert revoked.active is False
    assert revoked.revoked_at is not None
    assert store.revoke("web-search", "ops-agent", "v1") == revoked
    assert store.list_grants("web-search") == []

    with pytest.raises(ValueError, match="tool not found"):
        store.grant("missing-tool", "ops-agent", "v1")
    with pytest.raises(ValueError, match="agent version not found"):
        store.grant("web-search", "missing-agent", "v1")
    with pytest.raises(ValueError, match="grant not found"):
        store.revoke("web-search", "missing-agent", "v1")

    events = [event["action"] for event in audit.read_events()]
    assert events == [
        "tool_registry.registered",
        "agent_registry.version_registered",
        "tool_registry.granted",
        "tool_registry.revoked",
    ]


def test_check_capability_matrix(store: ToolRegistryStore, agents: AgentRegistryStore) -> None:
    assert store.check_capability("missing-agent", "v1", "missing-tool") is False
    store.register(_tool())
    _agent(agents)
    assert store.check_capability("ops-agent", "v1", "web-search") is False
    store.grant("web-search", "ops-agent", "v1")
    assert store.check_capability("ops-agent", "v1", "web-search") is True
    store.set_tool_enabled("web-search", enabled=False)
    assert store.check_capability("ops-agent", "v1", "web-search") is False
    assert store.list_grants("web-search") == []

    store.set_tool_enabled("web-search", enabled=True)
    store.grant("web-search", "ops-agent", "v1")
    _agent(agents, version="v2", prohibited_tools=["web-search"])
    store.grant("web-search", "ops-agent", "v2")
    assert store.check_capability("ops-agent", "v2", "web-search") is False
    assert store.check_capability("ops-agent", "v1", "web-search") is True


def test_disabled_tool_drops_grants_and_revalidation_audits(
    store: ToolRegistryStore, agents: AgentRegistryStore, audit_log: Path
) -> None:
    store.register(_tool("search-a"))
    store.register(_tool("search-b"))
    _agent(agents)
    for tool_id in ("search-a", "search-b"):
        store.grant(tool_id, "ops-agent", "v1")

    store.set_tool_enabled("search-a", enabled=False)
    assert store.list_grants("search-a") == []
    disabled_drops = store.set_tool_enabled("search-b", enabled=False)
    assert disabled_drops.enabled is False
    assert store.list_grants("search-b") == []

    # Recreate the disabled-tool state directly to exercise the revalidate helper.
    store.grant("search-a", "ops-agent", "v1")
    with store._sessions.begin() as session:
        row = store._tool(session, "search-a")
        assert row is not None
        row.enabled = False
    dropped = store.revalidate("search-a")
    assert [(grant.tool_id, grant.active) for grant in dropped] == [("search-a", False)]
    assert store.list_grants("search-a") == []
    events = [event["action"] for event in audit.read_events()]
    assert events.count("tool_registry.revoked") == 1
    assert events.count("tool_registry.disabled") == 2


def test_registry_routes_are_wired() -> None:
    names = {route.name for route in app.routes if isinstance(route, APIRoute)}
    assert {
        "get_tools",
        "register_tool",
        "grant_tool",
        "revoke_tool",
        "get_tool_agents",
        "revalidate_tool",
    } <= names


def test_grant_request_contract_is_agent_version_scoped() -> None:
    request = CapabilityGrantRequest(agent_id="ops-agent", version="v1")
    assert request.agent_id == "ops-agent"
    assert request.version == "v1"


def test_no_provider_model_identifiers_in_tool_registry_module() -> None:
    src = Path(tool_registry.__file__).read_text()
    for provider_token in ("gpt-", "claude-", "gemini-", "deepseek", "qwen"):
        assert provider_token not in src, provider_token


def test_agent_registry_contract_fields_align(
    store: ToolRegistryStore, agents: AgentRegistryStore
) -> None:
    _agent(agents, tool_grants=["web-search"])
    store.register(_tool())
    record = agents.get_agent("ops-agent")
    assert record is not None
    assert record.versions[0].tool_grants == ["web-search"]
    assert record.versions[0].prohibited_tools == []
    assert AgentState.DRAFT.value in {"DRAFT"}
