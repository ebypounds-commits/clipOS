"""Regression: registering an agent version must not violate its own FK.

The register path queued `agent_versions` before `agent_records` and then
called `session.get`, whose autoflush INSERTed the child first. SQLite does
not enforce foreign keys, so the suite passed while a real Postgres run
returned 500 with `agent_versions_agent_ref_fkey` — which is exactly what
happened the first time the app registered an agent through the API.

This test turns on SQLite's FK enforcement so the ordering bug is visible
without needing Postgres.
"""

from __future__ import annotations

import pytest
from sqlalchemy import StaticPool, create_engine, event
from sqlalchemy.engine import Engine

from clipos_api import agent_registry as ar
from clipos_api.agent_registry import (
    AgentClass,
    AgentRegistryStore,
    AgentState,
    AgentVersion,
    RiskClassification,
)


def _definition() -> AgentVersion:
    return AgentVersion(
        name="Research Worker",
        purpose="Collect and summarize factual research",
        agent_class=AgentClass.RESEARCH,
        prompt_version_ref="prompts/research/v1",
        model_alias="chat",
        fallback_aliases=[],
        required_capabilities=ar.RequiredCapabilities(
            reasoning=True, structured_output=True, min_context_tokens=128_000, tool_use=True
        ),
        tool_grants=["content.read"],
        prohibited_tools=["secrets.read_raw"],
        memory_policy={"scopes": ["run", "workflow"], "retention_days": 30},
        max_iterations=10,
        max_tool_calls=20,
        timeout_seconds=60,
        spend_ceilings=ar.SpendCeilings(per_run=0.10, hourly=1.0, daily=10.0, monthly=100.0),
        concurrency_ceiling=5,
        evaluators=[ar.EvaluatorRequirement(name="factuality", required_score=0.9)],
        retry_policy={"max_attempts": 2, "backoff_seconds": 1},
        escalation_policy={"target": "governance", "notify": ["governance"]},
        risk_classification=RiskClassification.LOW,
        activation_state=AgentState.DRAFT,
        canary_percentage=0,
    )


@pytest.fixture()
def enforcing_engine() -> Engine:
    """SQLite engine with foreign keys enforced, matching Postgres."""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    import clipos_api.main  # noqa: F401 - registers all metadata
    from clipos_api.repository import Base

    Base.metadata.create_all(engine)
    return engine


def test_register_version_creates_agent_root_before_the_version(
    enforcing_engine: Engine,
) -> None:
    store = AgentRegistryStore.from_engine(enforcing_engine)

    # With FK enforcement on, the old ordering raised IntegrityError here
    # (agent_versions.agent_ref has no parent row yet).
    registered = store.register_version("fk-agent", "1", _definition())

    assert registered.version == "1"
    assert registered.name == "Research Worker"
    assert registered.activation_state is AgentState.DRAFT

    # The parent row must exist and the version must be visible via the
    # read paths that join through it.
    loaded = store.get_agent("fk-agent")
    assert loaded is not None
    assert loaded.active_version is None
    assert [v.version for v in loaded.versions] == ["1"]


def test_second_registration_still_short_circuits(
    enforcing_engine: Engine,
) -> None:
    """The existing-object fast path must not regress the ordering fix."""
    store = AgentRegistryStore.from_engine(enforcing_engine)
    first = store.register_version("fk-agent-b", "1", _definition())
    second = store.register_version("fk-agent-b", "1", _definition())
    assert first.version == second.version == "1"
    assert second.name == first.name


def test_fk_enforcement_is_really_on(enforcing_engine: Engine) -> None:
    """Guard the guard: a no-op pragma would make the tests vacuous."""
    with enforcing_engine.connect() as conn:
        pragma = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert pragma == 1, "foreign keys are not enforced, the regression is invisible"
