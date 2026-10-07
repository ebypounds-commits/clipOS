"""Tests for gateway cost ledger, dispatch persistence, and recovery (CLIP-024)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit, llm_cost, model_registry
from clipos_api.llm_cost import (
    CostLedger,
    DispatchOutcome,
    DispatchRecord,
)
from clipos_api.llm_gateway import (
    AdapterRegistry,
    ChatMessage,
    LLMDispatchError,
    LLMGateway,
    LLMRequest,
    LLMResponse,
    ProviderAdapter,
)
from clipos_api.main import app
from clipos_api.model_registry import HealthState, ModelRegistryStore, ModelRoute


class FakeAdapter(ProviderAdapter):
    """Deterministic adapter whose fail flag can be flipped mid-test."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.fail = False
        self.calls: list[LLMRequest] = []

    def send(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self.fail:
            raise RuntimeError(f"{self.provider} unavailable")
        return LLMResponse(
            content=f"answer from {self.provider}", usage={"input_tokens": 3, "output_tokens": 5}
        )


class MutableClock:
    """Injectable test clock: real time, controllable offset."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    model_registry.Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-024-llm-cost-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ModelRegistryStore:
    return ModelRegistryStore.from_engine(engine)


@pytest.fixture()
def ledger(engine: Engine, audit_log: Path) -> CostLedger:
    return CostLedger.from_engine(engine)


@pytest.fixture()
def clock() -> MutableClock:
    return MutableClock()


def _route(alias: str, provider: str, priority: int) -> ModelRoute:
    return ModelRoute(
        alias=alias,
        provider=provider,
        endpoint_ref=f"https://{provider}.internal/infer",
        model_ref=f"ref-{provider}",
        priority=priority,
    )


def _record(
    execution_id: str = "exec-1",
    provider: str = "primary",
    outcome: DispatchOutcome = DispatchOutcome.SUCCEEDED,
    at: datetime | None = None,
    cost: float = 0.0,
) -> DispatchRecord:
    return DispatchRecord(
        execution_id=execution_id,
        alias="reasoning-standard",
        provider=provider,
        model_ref=f"ref-{provider}",
        outcome=outcome,
        latency_ms=12,
        usage={"input_tokens": 3, "output_tokens": 5},
        cost_estimate=cost,
        at=at or datetime.now(UTC),
    )


def test_record_is_idempotent_by_execution_and_route(ledger: CostLedger) -> None:
    first = ledger.record(_record())
    second = ledger.record(_record())
    assert first == second
    rows = ledger.recent_dispatches("reasoning-standard")
    assert len(rows) == 1
    assert rows[0].outcome is DispatchOutcome.SUCCEEDED


def test_sum_costs_filters_by_alias_and_window(ledger: CostLedger, clock: MutableClock) -> None:
    now = clock.now()
    ledger.record(_record(provider="primary", at=now, cost=1.5))
    ledger.record(_record(provider="backup", at=now - timedelta(hours=2), cost=2.0))
    ledger.record(
        _record(
            execution_id="exec-old", provider="primary", at=now - timedelta(hours=30), cost=99.0
        )
    )

    window = timedelta(hours=24)
    assert ledger.sum_costs("reasoning-standard", window) == pytest.approx(3.5)
    assert ledger.sum_costs("other-alias", window) == 0.0
    assert ledger.sum_costs("reasoning-standard", timedelta(hours=1)) == pytest.approx(1.5)


def test_sum_costs_rejects_non_positive_window(ledger: CostLedger) -> None:
    with pytest.raises(ValueError, match="window"):
        ledger.sum_costs("reasoning-standard", timedelta(0))


def test_fallback_chain_records_one_row_per_attempted_route(
    store: ModelRegistryStore, ledger: CostLedger, clock: MutableClock
) -> None:
    store.register(_route("reasoning-standard", "primary", 1))
    store.register(_route("reasoning-standard", "backup", 2))
    gateway = LLMGateway(
        store,
        AdapterRegistry(),
        ledger=ledger,
        clock=clock.now,
    )
    primary = FakeAdapter("primary")
    primary.fail = True
    backup = FakeAdapter("backup")
    gateway.register_adapter("primary", lambda route: primary)
    gateway.register_adapter("backup", lambda route: backup)

    response = gateway.dispatch(
        LLMRequest(
            alias="reasoning-standard",
            messages=[ChatMessage(role="user", content="hello")],
            execution_id="exec-1",
        )
    )

    assert response.provider == "backup"
    rows = ledger.recent_dispatches("reasoning-standard")
    assert [(r.provider, r.outcome) for r in rows] == [
        ("backup", DispatchOutcome.DEGRADED_FALLBACK),
        ("primary", DispatchOutcome.FAILED),
    ]
    failed = rows[1]
    assert failed.usage.input_tokens == 0
    assert failed.execution_id == "exec-1"


def test_degraded_route_excluded_within_cooldown_and_recovers_after(
    store: ModelRegistryStore, ledger: CostLedger, clock: MutableClock
) -> None:
    store.register(_route("reasoning-standard", "primary", 1))
    gateway = LLMGateway(store, AdapterRegistry(), ledger=ledger, clock=clock.now)
    adapter = FakeAdapter("primary")
    gateway.register_adapter("primary", lambda route: adapter)

    adapter.fail = True
    with pytest.raises(LLMDispatchError):
        gateway.dispatch(_request("exec-degraded"))
    routes = store.list_routes("reasoning-standard")
    assert routes[0].health_state is HealthState.DEGRADED

    # Still inside the 5-minute cooldown: no route is eligible.
    clock.advance(60)
    with pytest.raises(LLMDispatchError):
        gateway.dispatch(_request("exec-too-soon"))

    # After the cooldown the route is re-admitted; success restores health=ok.
    clock.advance(241)
    adapter.fail = False
    response = gateway.dispatch(_request("exec-recovered"))
    assert response.provider == "primary"
    routes = store.list_routes("reasoning-standard")
    assert routes[0].health_state is HealthState.OK
    rows = ledger.recent_dispatches("reasoning-standard")
    recovered = [r for r in rows if r.execution_id == "exec-recovered"]
    assert recovered and recovered[0].outcome is DispatchOutcome.SUCCEEDED


def _request(execution_id: str) -> LLMRequest:
    return LLMRequest(
        alias="reasoning-standard",
        messages=[ChatMessage(role="user", content="hello")],
        execution_id=execution_id,
    )


def test_gateway_ledger_failure_never_masks_dispatch(
    store: ModelRegistryStore, engine: Engine, audit_log: Path, clock: MutableClock
) -> None:
    store.register(_route("reasoning-standard", "primary", 1))

    class BrokenLedger:
        def record(self, record: DispatchRecord) -> DispatchRecord:
            raise RuntimeError("ledger down")

    gateway = LLMGateway(
        store,
        AdapterRegistry(),
        ledger=BrokenLedger(),  # type: ignore[arg-type]
        clock=clock.now,
    )
    gateway.register_adapter("primary", lambda route: FakeAdapter("primary"))

    response = gateway.dispatch(_request("exec-1"))
    assert response.provider == "primary"


def test_no_hard_coded_provider_model_ids_in_cost_module() -> None:
    source = Path(llm_cost.__file__).read_text(encoding="utf-8").lower()
    for marker in ("gpt-", "claude-", "gemini-", "llama-", "mistral-"):
        assert marker not in source


def test_operator_cost_endpoints(
    ledger: CostLedger,
) -> None:
    ledger.record(_record(provider="primary", cost=1.25))
    ledger.record(
        _record(
            execution_id="exec-2",
            provider="backup",
            outcome=DispatchOutcome.FAILED,
        )
    )

    summary = llm_cost.get_alias_costs(ledger, "reasoning-standard", window_hours=24)
    assert summary.total_cost_estimate == pytest.approx(1.25)
    assert {r.provider for r in summary.recent} == {"primary", "backup"}

    rows = llm_cost.get_alias_dispatches(ledger, "reasoning-standard", limit=20)
    assert len(rows) == 2
    assert {r.outcome for r in rows} == {DispatchOutcome.SUCCEEDED, DispatchOutcome.FAILED}


def test_operator_endpoints_validate_window_and_limit(ledger: CostLedger) -> None:
    import fastapi

    for window_hours in (0, 100000):
        with pytest.raises(fastapi.HTTPException) as exc:
            llm_cost.get_alias_costs(ledger, "reasoning-standard", window_hours=window_hours)
        assert exc.value.status_code == 400
    with pytest.raises(fastapi.HTTPException) as exc:
        llm_cost.get_alias_dispatches(ledger, "reasoning-standard", limit=0)
    assert exc.value.status_code == 400


def test_unknown_alias_costs_are_zero(ledger: CostLedger) -> None:
    summary = llm_cost.get_alias_costs(ledger, "unknown-alias")
    assert summary.total_cost_estimate == 0.0


def test_cost_endpoints_are_wired_by_identity() -> None:
    routes = {route.name: route for route in app.routes if isinstance(route, APIRoute)}
    assert "get_model_alias_costs" in routes
    assert "get_model_alias_dispatches" in routes
    assert routes["get_model_alias_costs"].endpoint is llm_cost.get_alias_costs
    assert routes["get_model_alias_dispatches"].endpoint is llm_cost.get_alias_dispatches
    paths = [route.path for route in app.routes if isinstance(route, APIRoute)]
    assert "/models/{alias}/costs" in paths
    assert "/models/{alias}/dispatches" in paths
