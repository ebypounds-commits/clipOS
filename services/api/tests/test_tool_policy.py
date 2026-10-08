"""Tests for tool sandbox, rate-limit, and audit policy (CLIP-029)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit, tool_policy
from clipos_api.agent_registry import (
    AgentClass,
    AgentRegistryStore,
    AgentVersion,
    RequiredCapabilities,
    RiskClassification,
    SpendCeilings,
)
from clipos_api.tool_policy import (
    SandboxPolicy,
    ToolPolicyDecisionReason,
    ToolPolicyEngine,
    ToolPolicyRequest,
)
from clipos_api.tool_registry import ToolDef, ToolRegistryStore


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api import agent_registry, tool_registry

    agent_registry.Base.metadata.create_all(e)
    tool_registry.Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-029-tool-policy-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def tools(engine: Engine, audit_log: Path) -> ToolRegistryStore:
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
        "metadata": {},
    }
    values.update(changes)
    return ToolDef.model_validate(values)


def _definition(tool_grants: list[str], prohibited: list[str]) -> AgentVersion:
    return AgentVersion(
        name="runner",
        purpose="exercise tool policy",
        agent_class=AgentClass.OPERATIONS,
        prompt_version_ref="prompt-v1",
        model_alias="reasoning-standard",
        required_capabilities=RequiredCapabilities(),
        tool_grants=tool_grants,
        prohibited_tools=prohibited,
        memory_policy={},
        max_iterations=1,
        max_tool_calls=10,
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
    tool_grants: list[str],
    prohibited: list[str] | None = None,
) -> None:
    agents_store.register_version("ops-agent", "v1", _definition(tool_grants, prohibited or []))


def _request(now: datetime, tool_id: str = "web-search") -> ToolPolicyRequest:
    return ToolPolicyRequest(
        agent_id="ops-agent",
        agent_version="v1",
        tool_id=tool_id,
        execution_id="exec-1",
        now=now,
    )


def _sandbox(**changes: object) -> SandboxPolicy:
    values = {
        "allowed_capabilities": ["network.search"],
        "network_access": True,
        "filesystem_scopes": [],
        "max_calls_per_window": 2,
        "window_seconds": 60.0,
    }
    values.update(changes)
    return SandboxPolicy.model_validate(values)


def _engine(
    tools: ToolRegistryStore,
    agents: AgentRegistryStore,
    sandbox: SandboxPolicy,
) -> ToolPolicyEngine:
    _agent(agents, ["web-search"])
    tools.grant("web-search", "ops-agent", "v1")
    return ToolPolicyEngine(tools, lambda request: sandbox)


def test_capability_pass_through_and_sandbox_are_audited(
    tools: ToolRegistryStore, agents: AgentRegistryStore, audit_log: Path
) -> None:
    tools.register(_tool())
    policy = _engine(tools, agents, _sandbox())
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    request = _request(now)
    assert policy.evaluate(request).allowed is True
    assert policy.record_call(request).allowed is True
    actions = [event["action"] for event in audit.read_events(audit_log)]
    assert "tool_policy.call_recorded" in actions
    assert "tool_policy.denied" not in actions


def test_rate_limit_blocks_and_resets_after_window(
    tools: ToolRegistryStore, agents: AgentRegistryStore, audit_log: Path
) -> None:
    tools.register(_tool())
    policy = _engine(tools, agents, _sandbox(max_calls_per_window=2))
    start = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    for offset in range(2):
        assert policy.evaluate(_request(start + timedelta(seconds=offset))).allowed is True
        policy.record_call(_request(start + timedelta(seconds=offset)))
    denied = policy.evaluate(_request(start + timedelta(seconds=2)))
    assert denied.allowed is False
    assert denied.reason is ToolPolicyDecisionReason.RATE_LIMITED
    allowed = policy.evaluate(_request(start + timedelta(seconds=61)))
    assert allowed.allowed is True
    reasons = [
        event["details"]["reason"]
        for event in audit.read_events(audit_log)
        if event["action"] == "tool_policy.denied"
    ]
    assert reasons == ["rate_limited"]


def test_sandbox_violation_is_denied_and_audited(
    tools: ToolRegistryStore, agents: AgentRegistryStore, audit_log: Path
) -> None:
    tools.register(_tool(capability_scopes=["filesystem.write"]))
    _agent(agents, ["filesystem.write"])
    policy = ToolPolicyEngine(
        tools, lambda request: _sandbox(allowed_capabilities=["network.search"])
    )
    decision = policy.evaluate(_request(datetime(2026, 10, 8, 12, 0, tzinfo=UTC)))
    assert decision.allowed is False
    assert decision.reason is ToolPolicyDecisionReason.SANDBOX_VIOLATION
    denial = audit.read_events(audit_log)[-1]
    assert denial["action"] == "tool_policy.denied"
    assert denial["details"]["reason"] == "sandbox_violation"


def test_tool_disabled_denied(
    tools: ToolRegistryStore, agents: AgentRegistryStore, audit_log: Path
) -> None:
    tools.register(_tool())
    policy = _engine(tools, agents, _sandbox())
    tools.set_tool_enabled("web-search", False)
    decision = policy.evaluate(_request(datetime(2026, 10, 8, 12, 0, tzinfo=UTC)))
    assert decision.allowed is False
    assert decision.reason is ToolPolicyDecisionReason.TOOL_DISABLED
    denial = audit.read_events(audit_log)[-1]
    assert denial["action"] == "tool_policy.denied"
    assert denial["details"]["reason"] == "tool_disabled"


def test_capability_denial_and_missing_tool(
    tools: ToolRegistryStore, agents: AgentRegistryStore, audit_log: Path
) -> None:
    tools.register(_tool())
    _agent(agents, [])
    policy = ToolPolicyEngine(tools, lambda request: _sandbox())
    denied = policy.evaluate(_request(datetime(2026, 10, 8, 12, 0, tzinfo=UTC)))
    assert denied.reason is ToolPolicyDecisionReason.CAPABILITY_DENIED
    missing = policy.evaluate(_request(datetime(2026, 10, 8, 12, 0, tzinfo=UTC), tool_id="missing"))
    assert missing.reason is ToolPolicyDecisionReason.TOOL_NOT_FOUND


def test_network_filesystem_sandbox_violations(
    tools: ToolRegistryStore, agents: AgentRegistryStore, audit_log: Path
) -> None:
    tools.register(_tool(metadata={"network_access": True, "filesystem_scopes": ["/tmp"]}))
    _agent(agents, ["web-search"])
    policy = ToolPolicyEngine(
        tools,
        lambda request: _sandbox(
            network_access=False,
            filesystem_scopes=["/other"],
        ),
    )
    decision = policy.evaluate(_request(datetime(2026, 10, 8, 12, 0, tzinfo=UTC)))
    assert decision.reason is ToolPolicyDecisionReason.SANDBOX_VIOLATION


def test_build_factory_requires_sqlalchemy_engine() -> None:
    with pytest.raises(TypeError):
        tool_policy.build_tool_policy(object(), lambda request: _sandbox())
