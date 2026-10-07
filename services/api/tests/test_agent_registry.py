"""Behavioral tests for the Agent Registry contracts, store, migration, and routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import create_engine

from clipos_api import agent_registry, audit, main
from clipos_api.agent_registry import (
    AgentClass,
    AgentRegistryStore,
    AgentState,
    AgentVersion,
    ModelAliasTarget,
    RiskClassification,
)


def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AgentRegistryStore:
    """Create an isolated SQLite registry with a fresh audit log."""
    audit.AUDIT_PATH = tmp_path / "agent-registry-audit.log"
    audit.AUDIT_PATH.write_text("", encoding="utf-8")
    engine = create_engine("sqlite:///:memory:")
    agent_registry.AgentVersionRecord.metadata.create_all(engine)
    agent_registry.LifecycleEventRecord.metadata.create_all(engine)
    return AgentRegistryStore.from_engine(engine)


def definition(risk: RiskClassification = RiskClassification.LOW) -> AgentVersion:
    """Return a complete, valid low-risk definition for tests."""
    return AgentVersion(
        name="Research Worker",
        purpose="Collect and summarize factual research",
        agent_class=AgentClass.RESEARCH,
        prompt_version_ref="prompts/research/v1",
        model_alias="reasoning-standard",
        fallback_aliases=["research-fast"],
        required_capabilities=agent_registry.RequiredCapabilities(
            reasoning=True, structured_output=True, min_context_tokens=128_000, tool_use=True
        ),
        tool_grants=["content.read"],
        prohibited_tools=["secrets.read_raw"],
        memory_policy={"scopes": ["run", "workflow"], "retention_days": 30},
        max_iterations=10,
        max_tool_calls=20,
        timeout_seconds=60,
        spend_ceilings=agent_registry.SpendCeilings(
            per_run=0.10, hourly=1.0, daily=10.0, monthly=100.0
        ),
        concurrency_ceiling=5,
        evaluators=[agent_registry.EvaluatorRequirement(name="factuality", required_score=0.9)],
        retry_policy={"max_attempts": 2, "backoff_seconds": 1},
        escalation_policy={"target": "governance", "notify": ["governance"]},
        risk_classification=risk,
    )


def actions() -> list[str]:
    return [item["action"] for item in audit.read_events()]


def test_contract_fields_are_complete() -> None:
    value = definition()
    assert value.activation_state is AgentState.DRAFT
    assert value.canary_percentage == 0
    assert value.agent_class is AgentClass.RESEARCH
    assert set(AgentClass._member_names_) == {
        "RESEARCH",
        "PLANNING",
        "CREATIVE",
        "MEDIA_ANALYSIS",
        "COMPLIANCE",
        "PUBLISHING",
        "ANALYTICS",
        "FINANCE",
        "OPERATIONS",
        "GOVERNANCE",
        "ENGINEERING_PROPOSAL",
        "EVALUATION",
    }


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AgentState.DRAFT, AgentState.ACTIVE),
        (AgentState.DRAFT, AgentState.SUSPENDED),
        (AgentState.EVALUATING, AgentState.ACTIVE),
        (AgentState.EVALUATING, AgentState.DRAFT),
        (AgentState.CANARY, AgentState.EVALUATING),
        (AgentState.CANARY, AgentState.SUSPENDED),
        (AgentState.ACTIVE, AgentState.RETIRED),
        (AgentState.RETIRED, AgentState.ACTIVE),
    ],
)
def test_illegal_lifecycle_transitions_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, current: AgentState, target: AgentState
) -> None:
    registry = store(monkeypatch, tmp_path)
    registered = registry.register_version("research", "v1", definition())
    assert registered.activation_state is AgentState.DRAFT

    # Legally advance to the requested current state when it is later in the chain.
    sequence = [
        AgentState.DRAFT,
        AgentState.EVALUATING,
        AgentState.CANARY,
        AgentState.ACTIVE,
        AgentState.SUSPENDED,
        AgentState.RETIRED,
    ]
    for next_state in sequence[1 : sequence.index(current) + 1]:
        registry.transition("research", "v1", next_state)
    with pytest.raises(ValueError, match="illegal lifecycle transition"):
        registry.transition("research", "v1", target)


def test_full_lifecycle_and_canary_percentage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = store(monkeypatch, tmp_path)
    registry.register_version("research", "v1", definition())
    assert registry.resolve("research") is None

    assert registry.transition("research", "v1", AgentState.EVALUATING).activation_state is (
        AgentState.EVALUATING
    )
    canary = registry.transition("research", "v1", AgentState.CANARY)
    assert canary.canary_percentage == 1
    assert registry.set_canary_percentage("research", "v1", 25).canary_percentage == 25
    assert registry.set_canary_percentage("research", "v1", 25).canary_percentage == 25

    active = registry.activate("research", "v1")
    assert active.activation_state is AgentState.ACTIVE
    resolved = registry.resolve("research")
    assert resolved is not None and resolved.version is not None

    assert registry.transition("research", "v1", AgentState.SUSPENDED).activation_state is (
        AgentState.SUSPENDED
    )
    assert registry.resolve("research") is None
    assert registry.transition("research", "v1", AgentState.RETIRED).activation_state is (
        AgentState.RETIRED
    )
    assert "agent_registry.canary_percentage_changed" in actions()
    assert actions().count("agent_registry.state_changed") == 5


def test_high_risk_activation_requires_explicit_human_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = store(monkeypatch, tmp_path)
    registry.register_version("rights", "v1", definition(RiskClassification.HIGH))
    registry.transition("rights", "v1", AgentState.EVALUATING)
    registry.transition("rights", "v1", AgentState.CANARY)
    with pytest.raises(ValueError, match="requires explicit human approval"):
        registry.activate("rights", "v1")
    approved = registry.activate("rights", "v1", approved_high_risk=True)
    assert approved.activation_state is AgentState.ACTIVE
    tail_actions = [item["action"] for item in audit.read_events()[-2:]]
    assert tail_actions == [
        "agent_registry.state_changed",
        "agent_registry.active_pointer_changed",
    ]
    state_event = audit.read_events()[-2]
    assert state_event["details"]["approved_high_risk"] is True


def test_register_version_is_idempotent_and_conflicts_are_immutable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = store(monkeypatch, tmp_path)
    first = registry.register_version("research", "v1", definition())
    first_actions = actions()
    assert first_actions == ["agent_registry.version_registered"]

    second = registry.register_version("research", "v1", definition())
    assert second == first
    assert actions() == first_actions

    conflicting = definition().model_copy(update={"purpose": "Different immutable purpose"})
    with pytest.raises(ValueError, match="different definition"):
        registry.register_version("research", "v1", conflicting)
    assert actions() == first_actions
    with pytest.raises(ValueError, match="DRAFT"):
        registry.register_version(
            "research",
            "v2",
            definition().model_copy(update={"activation_state": AgentState.ACTIVE}),
        )


def test_model_alias_resolution_is_ordered_and_filters_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = store(monkeypatch, tmp_path)
    targets = [
        ModelAliasTarget(
            alias="reasoning-standard",
            target_id="second",
            provider="logical-adapter-a",
            model_id="deployment-2",
            priority=2,
            enabled=True,
        ),
        ModelAliasTarget(
            alias="reasoning-standard",
            target_id="first",
            provider="logical-adapter-a",
            model_id="deployment-1",
            priority=1,
            enabled=True,
        ),
        ModelAliasTarget(
            alias="reasoning-standard",
            target_id="disabled",
            provider="logical-adapter-b",
            model_id="deployment-3",
            priority=0,
            enabled=False,
        ),
    ]
    registry.set_alias_targets(targets)
    resolved = registry.resolve_model_alias("reasoning-standard")
    assert [item.target_id for item in resolved] == ["first", "second"]
    assert all(item.enabled for item in resolved)
    assert "agent_registry.alias_targets_changed" in actions()


def test_alias_targets_are_replaced_by_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = store(monkeypatch, tmp_path)
    registry.set_alias_targets(
        [
            ModelAliasTarget(
                alias="reasoning-standard",
                target_id="old",
                provider="logical-adapter-a",
                model_id="deployment-old",
                priority=1,
            )
        ]
    )
    registry.set_alias_targets(
        [
            ModelAliasTarget(
                alias="reasoning-standard",
                target_id="new",
                provider="logical-adapter-a",
                model_id="deployment-new",
                priority=1,
            )
        ]
    )
    assert [item.target_id for item in registry.resolve_model_alias("reasoning-standard")] == [
        "new"
    ]


def test_agent_record_returns_versions_and_active_pointer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = store(monkeypatch, tmp_path)
    registry.register_version("research", "v1", definition())
    registry.register_version("research", "v2", definition().model_copy(update={"name": "Next"}))
    registry.transition("research", "v1", AgentState.EVALUATING)
    registry.transition("research", "v1", AgentState.CANARY)
    registry.activate("research", "v1")
    record = registry.get_agent("research")
    assert record is not None
    assert record.agent_id == "research"
    assert record.active_version == "v1"
    assert [item.version for item in record.versions] == ["v1", "v2"]
    assert record.versions[0].activation_state is AgentState.ACTIVE
    assert record.versions[1].activation_state is AgentState.DRAFT
    assert registry.get_agent("missing") is None


def test_routes_are_wired_by_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    assert agent_registry.router.routes
    for route in agent_registry.router.routes:
        assert any(candidate is route for candidate in main.app.routes)
    names = {
        item.name: item.path
        for item in main.app.routes
        if isinstance(item, APIRoute) and item.path.startswith("/agents")
    }
    assert names == {
        "register_agent_version": "/agents",
        "list_agents": "/agents",
        "get_agent": "/agents/{agent_id}",
        "activate_agent": "/agents/{agent_id}/activate",
        "resolve_agent": "/agents/{agent_id}/resolve",
        "promote_agent_version": "/agents/{agent_id}/promote",
        "get_agent_lifecycle": "/agents/{agent_id}/lifecycle",
    }


def test_no_provider_model_identifiers_in_agent_definitions() -> None:
    value = definition()
    assert value.model_alias == "reasoning-standard"
    assert value.fallback_aliases == ["research-fast"]


def test_lifecycle_history_records_every_transition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each legal transition appends a durable, ordered lifecycle event."""
    registry = store(monkeypatch, tmp_path)
    registry.register_version("research", "v1", definition())
    for target in (AgentState.EVALUATING, AgentState.CANARY, AgentState.ACTIVE):
        registry.transition("research", "v1", target, actor="ops-operator")

    history = registry.lifecycle_history("research")
    assert [item.from_state for item in history] == [
        AgentState.DRAFT,
        AgentState.EVALUATING,
        AgentState.CANARY,
    ]
    assert [item.to_state for item in history] == [
        AgentState.EVALUATING,
        AgentState.CANARY,
        AgentState.ACTIVE,
    ]
    assert all(item.agent_id == "research" and item.version == "v1" for item in history)
    assert all(item.actor == "ops-operator" for item in history)
    assert history == registry.lifecycle_history("research", "v1")
    assert registry.lifecycle_history("missing") == []


def test_list_agents_returns_registry_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The registry list exposes every agent with active pointer and versions."""
    registry = store(monkeypatch, tmp_path)
    assert registry.list_agents() == []
    registry.register_version("research", "v1", definition())
    registry.register_version("research", "v2", definition())
    registry.transition("research", "v1", AgentState.EVALUATING)

    agents = registry.list_agents()
    assert len(agents) == 1
    assert agents[0].agent_id == "research"
    assert agents[0].active_version is None
    assert [item.version for item in agents[0].versions] == ["v1", "v2"]


def test_agent_list_and_lifecycle_routes_are_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator lifecycle and list routes are identity-present on the app."""
    for route in agent_registry.router.routes:
        assert any(candidate is route for candidate in main.app.routes)
    by_path = {route.path: route for route in main.app.routes if isinstance(route, APIRoute)}
    lifecycle = by_path["/agents/{agent_id}/lifecycle"]
    listing = by_path["/agents"]
    assert lifecycle.methods == {"GET"}
    assert listing.methods == {"GET"}
