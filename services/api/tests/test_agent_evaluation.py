"""Tests for agent evaluation hooks and execution audit (CLIP-021)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import agent_evaluation, agent_registry, audit
from clipos_api.agent_evaluation import (
    AgentEvaluationStore,
    ExecutionAuditRecord,
    ExecutionOutcome,
    RecordEvaluationRequest,
)
from clipos_api.agent_registry import AgentClass, AgentState, RiskClassification
from clipos_api.main import app


@pytest.fixture()
def engine() -> Engine:
    e = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    agent_registry.Base.metadata.create_all(e)
    return e


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> agent_registry.AgentRegistryStore:
    monkeypatch.setattr(audit, "AUDIT_PATH", Path("/tmp") / "clipos-021-eval-audit.log")
    audit.AUDIT_PATH.write_text("", encoding="utf-8")
    store = agent_registry.AgentRegistryStore.from_engine(engine)
    store.register_version(
        "research",
        "v1",
        agent_registry.AgentVersion(
            name="Research Worker",
            purpose="Collect and summarize factual research",
            agent_class=AgentClass.RESEARCH,
            prompt_version_ref="prompts/research/v1",
            model_alias="reasoning-standard",
            fallback_aliases=[],
            required_capabilities=agent_registry.RequiredCapabilities(
                reasoning=True, structured_output=True, min_context_tokens=128_000, tool_use=True
            ),
            tool_grants=["content.read"],
            prohibited_tools=["secrets.read_raw"],
            memory_policy={"scopes": ["run"], "retention_days": 30},
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
            risk_classification=RiskClassification.LOW,
        ),
    )
    return store


@pytest.fixture()
def eval_store(engine: Engine) -> AgentEvaluationStore:
    return AgentEvaluationStore.from_engine(engine)


def test_record_evaluation_is_idempotent_and_scored(
    registry: agent_registry.AgentRegistryStore, eval_store: AgentEvaluationStore
) -> None:
    run = eval_store.record_evaluation(
        "research",
        RecordEvaluationRequest(version="v1", evaluator="factuality", score=0.95, run_id="run-1"),
    )
    assert run.passed is True
    again = eval_store.record_evaluation(
        "research",
        RecordEvaluationRequest(version="v1", evaluator="factuality", score=0.95, run_id="run-1"),
    )
    assert again == run
    with pytest.raises(ValueError, match="evaluator is not required"):
        eval_store.record_evaluation(
            "research",
            RecordEvaluationRequest(
                version="v1", evaluator="unknown-evaluator", score=1.0, run_id="run-2"
            ),
        )


def test_evaluation_gate_requires_recent_passing_runs(
    registry: agent_registry.AgentRegistryStore, eval_store: AgentEvaluationStore
) -> None:
    assert eval_store.evaluation_gate("research", "v1") is False
    eval_store.record_evaluation(
        "research",
        RecordEvaluationRequest(version="v1", evaluator="factuality", score=0.95, run_id="run-1"),
    )
    assert eval_store.evaluation_gate("research", "v1") is True
    eval_store.record_evaluation(
        "research",
        RecordEvaluationRequest(version="v1", evaluator="factuality", score=0.5, run_id="run-2"),
    )
    assert eval_store.evaluation_gate("research", "v1") is False


def test_activation_requires_evaluation_gate(
    registry: agent_registry.AgentRegistryStore, eval_store: AgentEvaluationStore
) -> None:
    registry.transition("research", "v1", AgentState.EVALUATING)
    registry.transition("research", "v1", AgentState.CANARY)
    with pytest.raises(ValueError, match="evaluation gate"):
        registry.activate("research", "v1")
    eval_store.record_evaluation(
        "research",
        RecordEvaluationRequest(version="v1", evaluator="factuality", score=0.95, run_id="run-1"),
    )
    active = registry.activate("research", "v1")
    assert active.activation_state is AgentState.ACTIVE


def test_execution_audit_start_finish_and_failed(
    registry: agent_registry.AgentRegistryStore, eval_store: AgentEvaluationStore
) -> None:
    record = ExecutionAuditRecord(
        agent_id="research",
        execution_id="exec-1",
        version="v1",
        model_alias="reasoning-standard",
        action="research.collect",
        started_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    started = eval_store.record_execution_start("research", "exec-1", record)
    assert started.outcome is ExecutionOutcome.RUNNING
    # idempotent re-start
    assert eval_store.record_execution_start("research", "exec-1", record) == started

    finished = eval_store.record_execution_finish("research", "exec-1", ExecutionOutcome.SUCCEEDED)
    assert finished.outcome is ExecutionOutcome.SUCCEEDED
    with pytest.raises(ValueError, match="error_class"):
        eval_store.record_execution_finish(
            "research", "exec-1", ExecutionOutcome.FAILED, error_class=None
        )


def test_execute_audited_closes_failed_execution(
    registry: agent_registry.AgentRegistryStore, eval_store: AgentEvaluationStore
) -> None:
    with (
        pytest.raises(RuntimeError, match="boom"),
        eval_store.execute_audited(
            "research", "exec-2", "v1", "reasoning-standard", "research.collect"
        ),
    ):
        raise RuntimeError("boom")
    rows = eval_store.list_executions("research")
    assert len(rows) == 1
    assert rows[0].outcome is ExecutionOutcome.FAILED
    assert rows[0].error_class == "RuntimeError"


def test_no_provider_model_identifiers_in_evaluation_module() -> None:
    src = Path(agent_evaluation.__file__).read_text()
    for provider_token in ("gpt-", "claude-", "gemini-", "deepseek", "qwen"):
        assert provider_token not in src, provider_token


def test_evaluation_routes_are_wired() -> None:
    names = {route.name for route in app.routes if isinstance(route, APIRoute)}
    assert {
        "get_agent_evaluations",
        "record_agent_evaluation",
        "get_agent_executions",
    } <= names
