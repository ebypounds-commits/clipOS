"""Behavioral tests for agent policy, budget, promotion, and route wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import create_engine

from clipos_api import agent_policy, agent_registry, audit, main
from clipos_api.agent_policy import (
    AgentBudgetConfigurationRecord,
    AgentPolicyStore,
    BudgetConfiguration,
    EscalationPolicy,
    MemoryPolicy,
    MemoryScope,
    PolicyOverride,
    RetryPolicy,
)
from clipos_api.agent_registry import AgentRegistryStore, AgentState


def policy_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[AgentRegistryStore, AgentPolicyStore]:
    """Create an isolated SQLite registry plus policy store with a fresh audit log."""
    audit.AUDIT_PATH = tmp_path / "agent-policy-audit.log"
    audit.AUDIT_PATH.write_text("", encoding="utf-8")
    engine = create_engine("sqlite:///:memory:")
    agent_registry.AgentVersionRecord.metadata.create_all(engine)
    agent_policy.PolicyOverrideRecord.metadata.create_all(engine)
    agent_policy.AgentBudgetConfigurationRecord.metadata.create_all(engine)
    agent_policy.AgentBudgetConfigurationRecord.metadata.create_all(engine)
    return (
        AgentRegistryStore.from_engine(engine),
        AgentPolicyStore.from_engine(engine),
    )


def definition() -> agent_registry.AgentVersion:
    """Return a complete, valid low-risk definition."""
    return agent_registry.AgentVersion(
        name="Research Worker",
        purpose="Collect and summarize factual research",
        agent_class=agent_registry.AgentClass.RESEARCH,
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
        risk_classification=agent_registry.RiskClassification.LOW,
    )


def actions() -> list[str]:
    return [item["action"] for item in audit.read_events()]


def test_policy_contract_rejections() -> None:
    """Invalid retry, escalation, memory, and budget values are rejected."""
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0, backoff_seconds=1)
    with pytest.raises(ValueError, match="backoff_seconds"):
        RetryPolicy(max_attempts=1, backoff_seconds=-0.1)
    with pytest.raises(ValueError, match="known agent_class"):
        EscalationPolicy(target="unknown-class")
    assert EscalationPolicy(target="compliance").target == "compliance"
    assert EscalationPolicy(target="human").target == "human"
    with pytest.raises(ValueError, match="subset of allowed"):
        MemoryPolicy(allowed_scopes=[MemoryScope.RUN], default_scopes=[MemoryScope.BRAND])
    with pytest.raises(ValueError, match="ordered"):
        BudgetConfiguration(per_run=2.0, hourly=1.0)
    with pytest.raises(ValueError, match="ordered"):
        BudgetConfiguration(daily=20.0, monthly=19.0)
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        BudgetConfiguration(hourly=-1.0)
    assert BudgetConfiguration(per_run=1.0, monthly=10.0).daily is None


def test_promotion_creates_new_draft_and_preserves_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Promotion copies the definition into a new DRAFT and never mutates the source."""
    registry, _ = policy_store(monkeypatch, tmp_path)
    registry.register_version("research", "v1", definition())
    registry.transition("research", "v1", agent_registry.AgentState.EVALUATING)

    promoted = registry.promote_version("research", "v1", "v2")
    assert promoted.activation_state is AgentState.DRAFT
    assert promoted.version == "v2"
    source = registry.get_agent("research")
    assert source is not None
    assert [item.version for item in source.versions] == ["v1", "v2"]
    assert source.versions[0].activation_state is AgentState.EVALUATING
    assert agent_registry._definition_signature(promoted) == agent_registry._definition_signature(
        source.versions[0]
    )
    with pytest.raises(ValueError, match="not found"):
        registry.promote_version("research", "missing", "v3")
    assert "agent_registry.version_registered" in actions()


def test_budget_update_effective_dating_and_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Budgets are append-only, effective dated, idempotent, and audited."""
    registry, store = policy_store(monkeypatch, tmp_path)
    registry.register_version("research", "v1", definition())
    start = datetime.now(UTC)
    later = start + timedelta(hours=2)
    new_budget = BudgetConfiguration(per_run=0.2, hourly=2.0, daily=20.0, monthly=200.0)

    store.update_budget("research", "v1", new_budget, later)
    before_change = store.resolve("research", "v1", at=start)
    after_change = store.resolve("research", "v1", at=later + timedelta(minutes=1))
    assert before_change.budget.per_run == 0.10
    assert after_change.budget == new_budget
    assert "agent_policy.budget_changed" in actions()

    repeat = store.update_budget("research", "v1", new_budget, later)
    assert repeat == new_budget
    assert actions().count("agent_policy.budget_changed") == 1
    with pytest.raises(ValueError, match="after the current configuration start"):
        store.update_budget("research", "v1", new_budget, start + timedelta(minutes=1))


def test_override_resolution_precedence_and_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit overrides replace only supplied fields and are effective dated."""
    registry, store = policy_store(monkeypatch, tmp_path)
    registry.register_version("research", "v1", definition())
    default = store.resolve("research", "v1")
    assert default.override_applied is False
    assert default.retry_policy == RetryPolicy(max_attempts=2, backoff_seconds=1)
    assert default.budget == BudgetConfiguration(
        per_run=0.10, hourly=1.0, daily=10.0, monthly=100.0
    )

    effective_from = datetime.now(UTC) - timedelta(minutes=5)
    override = PolicyOverride(
        retry_policy=RetryPolicy(max_attempts=4, backoff_seconds=2),
        memory_policy=MemoryPolicy(
            allowed_scopes=[MemoryScope.RUN, MemoryScope.WORKFLOW],
            default_scopes=[MemoryScope.RUN],
            retention_days=7,
        ),
        effective_from=effective_from,
    )
    resolved = store.set_override("research", "v1", override)
    assert resolved.override_applied is True
    assert resolved.retry_policy == RetryPolicy(max_attempts=4, backoff_seconds=2)
    assert resolved.memory_policy.retention_days == 7
    assert resolved.escalation_policy == default.escalation_policy
    assert resolved.budget == default.budget
    assert "agent_policy.override_changed" in actions()

    untouched = store.resolve("research", "v1", at=effective_from - timedelta(minutes=1))
    assert untouched.override_applied is False
    assert untouched.retry_policy == default.retry_policy
    assert store.get_override("research", "v1") == override

    with pytest.raises(ValueError, match="not found"):
        store.set_override("research", "missing", override)


def test_override_budget_history_supersedes_default_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Effective budget history wins over the override budget after its window."""
    registry, store = policy_store(monkeypatch, tmp_path)
    registry.register_version("research", "v1", definition())
    start = datetime.now(UTC)
    change_at = start + timedelta(hours=1)
    store.set_override(
        "research",
        "v1",
        PolicyOverride(
            budget=BudgetConfiguration(hourly=1.5),
            effective_from=start - timedelta(minutes=1),
        ),
    )
    store.update_budget(
        "research",
        "v1",
        BudgetConfiguration(per_run=0.5, hourly=5.0, daily=50.0, monthly=500.0),
        change_at,
    )
    assert store.resolve("research", "v1", at=start).budget.hourly == 1.5
    assert store.resolve("research", "v1", at=change_at + timedelta(minutes=1)).budget.hourly == 5.0


def test_budget_history_rows_are_append_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A superseded budget keeps its window closed, not deleted or rewritten."""
    registry, store = policy_store(monkeypatch, tmp_path)
    registry.register_version("research", "v1", definition())
    first_at = datetime.now(UTC)
    first = BudgetConfiguration(hourly=1.5)
    store.update_budget("research", "v1", first, first_at)
    second_at = first_at + timedelta(hours=1)
    store.update_budget(
        "research",
        "v1",
        BudgetConfiguration(hourly=2.5),
        second_at,
    )
    with store._sessions() as session:
        rows = session.scalars(
            agent_policy.sa.select(AgentBudgetConfigurationRecord).order_by(
                AgentBudgetConfigurationRecord.effective_from
            )
        ).all()
    assert len(rows) == 2
    assert agent_policy._aware(rows[0].effective_to) == second_at
    assert rows[1].effective_to is None


def test_policy_routes_are_wired() -> None:
    """Policy and promotion routes are identity-checkable from the app."""
    names = {
        route.name
        for route in main.app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/agent-policies")
    }
    assert {
        "set_agent_policy_override",
        "resolve_agent_effective_policy",
        "update_agent_budget",
    } <= names
    router_paths = {
        route.path for route in agent_policy.router.routes if isinstance(route, APIRoute)
    }
    assert "/agent-policies/{agent_id}/{version}/policy-override" in router_paths


def test_suspended_and_retired_versions_reject_policy_and_budget_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lifecycle-frozen versions reject override and budget mutations with 409 semantics."""
    registry, policies = policy_store(monkeypatch, tmp_path)
    registry.register_version("research", "v1", definition())
    for target in (
        agent_registry.AgentState.EVALUATING,
        agent_registry.AgentState.CANARY,
        agent_registry.AgentState.ACTIVE,
        agent_registry.AgentState.SUSPENDED,
    ):
        registry.transition("research", "v1", target)

    override = PolicyOverride(
        retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=2),
        effective_from=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="SUSPENDED"):
        policies.set_override("research", "v1", override)
    with pytest.raises(ValueError, match="SUSPENDED"):
        policies.update_budget(
            "research",
            "v1",
            BudgetConfiguration(per_run=0.2, hourly=2.0, daily=20.0, monthly=200.0),
            datetime.now(UTC) + timedelta(hours=1),
        )

    registry.transition("research", "v1", agent_registry.AgentState.RETIRED)
    with pytest.raises(ValueError, match="RETIRED"):
        policies.set_override("research", "v1", override)

    # A pre-suspension override remains resolvable; the mutation was rejected, not applied.
    resolved = policies.resolve("research", "v1")
    assert resolved.override_applied is False


def test_active_version_still_accepts_policy_and_budget_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only SUSPENDED and RETIRED versions are lifecycle-frozen."""
    registry, policies = policy_store(monkeypatch, tmp_path)
    registry.register_version("research", "v1", definition())
    for target in (
        agent_registry.AgentState.EVALUATING,
        agent_registry.AgentState.CANARY,
        agent_registry.AgentState.ACTIVE,
    ):
        registry.transition("research", "v1", target)
    resolved = policies.set_override(
        "research",
        "v1",
        PolicyOverride(effective_from=datetime.now(UTC)),
    )
    assert resolved.override_applied is True
    policies.update_budget(
        "research",
        "v1",
        BudgetConfiguration(per_run=0.2, hourly=2.0, daily=20.0, monthly=200.0),
        datetime.now(UTC),
    )
