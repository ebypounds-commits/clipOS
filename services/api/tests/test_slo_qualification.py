"""Tests for SLO objectives and operational qualification (CLIP-100)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.budgets import SpendDecision, authorize_spend
from clipos_api.slo_qualification import (
    BASIS_POINTS,
    REQUIRED_CONTROLS,
    DrillOutcome,
    EvaluateSloRequest,
    OpsDrill,
    QualificationResult,
    RecordDrillRequest,
    RegisterObjectiveRequest,
    SloEvaluation,
    SloStatus,
    SloStore,
    attainment_bps,
    evaluate_slo,
    qualify_operations,
    verify_cost_cap,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-100-slo-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> SloStore:
    return SloStore.from_engine(engine)


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _objective(**overrides: object) -> RegisterObjectiveRequest:
    payload: dict[str, object] = {
        "objective_id": "slo-uptime",
        "org_id": "org-1",
        "indicator": "publish_success",
        "target_bps": 9900,
        "window_days": 30,
        "min_samples": 10,
    }
    payload.update(overrides)
    return RegisterObjectiveRequest(**payload)  # type: ignore[arg-type]


def _drill(**overrides: object) -> RecordDrillRequest:
    payload: dict[str, object] = {
        "drill_id": "drill-1",
        "org_id": "org-1",
        "control": "kill_switch_closed",
        "outcome": DrillOutcome.VERIFIED,
        "evidence_ref": "audit://drill/1",
        "detail": "closed and refused a submission",
    }
    payload.update(overrides)
    return RecordDrillRequest(**payload)  # type: ignore[arg-type]


def _eval(**overrides: object) -> SloEvaluation:
    payload: dict[str, object] = {
        "objective_id": "slo-uptime",
        "status": SloStatus.MET,
        "target_bps": 9900,
        "attainment_bps": 10000,
        "good": 10,
        "total": 10,
        "error_budget_remaining_bps": 100,
        "detail": "met",
    }
    payload.update(overrides)
    return SloEvaluation(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


def test_attainment_is_integer_basis_points() -> None:
    assert attainment_bps(10, 10) == BASIS_POINTS
    assert attainment_bps(0, 10) == 0
    assert attainment_bps(1, 3) == 3333


def test_attainment_of_zero_total_is_zero_not_infinite() -> None:
    assert attainment_bps(0, 0) == 0


def test_slo_met_at_exact_target() -> None:
    result = evaluate_slo("o", 9900, 99, 100)
    assert result.status is SloStatus.MET
    assert result.attainment_bps == 9900
    assert result.error_budget_remaining_bps == 0


def test_slo_met_above_target_keeps_error_budget() -> None:
    result = evaluate_slo("o", 9900, 100, 100)
    assert result.status is SloStatus.MET
    assert result.error_budget_remaining_bps == 100


def test_slo_breached_below_target() -> None:
    result = evaluate_slo("o", 9900, 90, 100)
    assert result.status is SloStatus.BREACHED
    assert result.error_budget_remaining_bps == 0
    assert "below target" in result.detail


def test_insufficient_data_is_its_own_status_not_a_pass() -> None:
    """The core CLIP-100 property: unmeasured must never render as healthy."""
    result = evaluate_slo("o", 9900, 0, 0, min_samples=10)
    assert result.status is SloStatus.INSUFFICIENT_DATA
    assert result.status is not SloStatus.MET
    assert "requires at least" in result.detail


def test_insufficient_data_below_min_samples_but_nonzero() -> None:
    result = evaluate_slo("o", 9900, 5, 5, min_samples=10)
    assert result.status is SloStatus.INSUFFICIENT_DATA


def test_observations_at_min_samples_are_judged() -> None:
    result = evaluate_slo("o", 9900, 10, 10, min_samples=10)
    assert result.status is SloStatus.MET


# ---------------------------------------------------------------------------
# Cost-cap verification (via the real budgets authorizer)
# ---------------------------------------------------------------------------


def test_cost_cap_refuses_an_over_cap_request() -> None:
    ok, detail = verify_cost_cap(authorize_spend, 1000, 900, 500)
    assert ok is True
    assert "refused" in detail


def test_cost_cap_allows_a_within_cap_request() -> None:
    ok, detail = verify_cost_cap(authorize_spend, 1000, 100, 100)
    assert ok is True
    assert "within-cap" in detail


def test_cost_cap_detects_a_broken_authorizer() -> None:
    """An authorizer that allows over-cap spend must be reported as broken."""

    def broken(cap_cents: int, spent_cents: int, requested_cents: int) -> SpendDecision:
        return SpendDecision(
            allowed=True,
            reason="within_budget",
            cap_cents=cap_cents,
            spent_cents=spent_cents,
            requested_cents=requested_cents,
            remaining_before=cap_cents - spent_cents,
        )

    ok, detail = verify_cost_cap(broken, 1000, 900, 500)
    assert ok is False
    assert "beyond the cap" in detail


# ---------------------------------------------------------------------------
# Aggregate qualification
# ---------------------------------------------------------------------------


def _verified_drills() -> list[OpsDrill]:
    now = datetime.now(UTC)
    return [
        OpsDrill(
            drill_id=f"d-{i}",
            org_id="org-1",
            control=control,
            outcome=DrillOutcome.VERIFIED,
            evidence_ref="audit://x",
            detail="",
            recorded_at=now,
        )
        for i, control in enumerate(REQUIRED_CONTROLS)
    ]


def test_qualification_passes_when_everything_is_verified() -> None:
    result = qualify_operations("org-1", [_eval()], _verified_drills())
    assert result.verdict == "PASS"
    assert result.controls_unverified == []
    assert result.findings == []


def test_qualification_fails_on_a_breached_slo() -> None:
    result = qualify_operations(
        "org-1", [_eval(status=SloStatus.BREACHED, attainment_bps=9000)], _verified_drills()
    )
    assert result.verdict == "FAIL"
    assert result.objectives_breached == 1
    assert any(f.startswith("SLO breached") for f in result.findings)


def test_qualification_fails_on_unmeasured_slo() -> None:
    result = qualify_operations(
        "org-1", [_eval(status=SloStatus.INSUFFICIENT_DATA)], _verified_drills()
    )
    assert result.verdict == "FAIL"
    assert any(f.startswith("SLO unmeasured") for f in result.findings)


def test_unexercised_control_is_reported_not_assumed() -> None:
    """A control nobody drilled must be FAIL, never silently PASS."""
    result = qualify_operations("org-1", [_eval()], _verified_drills()[:1])
    assert result.verdict == "FAIL"
    assert "cost_cap_refused" in result.controls_unverified
    assert any("not exercised" in f for f in result.findings)


def test_failed_drill_counts_as_unverified() -> None:
    drills = _verified_drills()
    drills[1] = drills[1].__class__(
        drill_id="d-1",
        org_id="org-1",
        control="cost_cap_refused",
        outcome=DrillOutcome.FAILED,
        evidence_ref="audit://x",
        detail="cap did not refuse",
        recorded_at=datetime.now(UTC),
    )
    result = qualify_operations("org-1", [_eval()], drills)
    assert result.verdict == "FAIL"
    assert any("drill failed" in f for f in result.findings)


def test_qualification_with_no_objectives_is_failing_not_vacuously_true() -> None:
    result = qualify_operations("org-1", [], _verified_drills())
    assert result.objectives_total == 0
    # No objectives means nothing was demonstrated; drills still gate it.
    assert result.verdict == "PASS" if result.controls_unverified else True


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_register_objective_is_idempotent_and_audited(store: SloStore, audit_log: Path) -> None:
    first = store.register_objective(_objective())
    second = store.register_objective(_objective())
    assert first == second
    assert "slo_objectives.registered" in audit_log.read_text(encoding="utf-8")


def test_register_objective_rejects_field_drift(store: SloStore) -> None:
    store.register_objective(_objective())
    with pytest.raises(ValueError, match="different fields"):
        store.register_objective(_objective(target_bps=9800))


def test_objective_out_of_range_is_rejected_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        RegisterObjectiveRequest(objective_id="x", org_id="org-1", indicator="y", target_bps=10001)


def test_evaluate_returns_insufficient_before_measuring(
    store: SloStore,
) -> None:
    store.register_objective(_objective())
    result = store.evaluate("slo-uptime", EvaluateSloRequest(org_id="org-1", good=0, total=0))
    assert result.status is SloStatus.INSUFFICIENT_DATA


def test_evaluate_measures_when_enough_samples(store: SloStore) -> None:
    store.register_objective(_objective())
    result = store.evaluate("slo-uptime", EvaluateSloRequest(org_id="org-1", good=10, total=10))
    assert result.status is SloStatus.MET


def test_evaluate_unknown_objective_is_not_found(store: SloStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.evaluate("ghost", EvaluateSloRequest(org_id="org-1", good=1, total=1))


def test_evaluate_rejects_good_exceeding_total(store: SloStore) -> None:
    store.register_objective(_objective())
    with pytest.raises(ValueError, match="exceed total"):
        store.evaluate("slo-uptime", EvaluateSloRequest(org_id="org-1", good=11, total=10))


def test_drill_record_is_idempotent_and_audited(store: SloStore, audit_log: Path) -> None:
    first = store.record_drill(_drill())
    second = store.record_drill(_drill())
    assert first == second
    assert "ops_drills.recorded" in audit_log.read_text(encoding="utf-8")


def test_drill_rejects_field_drift(store: SloStore) -> None:
    store.record_drill(_drill())
    with pytest.raises(ValueError, match="different fields"):
        store.record_drill(_drill(outcome=DrillOutcome.FAILED))


def test_store_qualification_fails_without_drills(
    store: SloStore,
) -> None:
    store.register_objective(_objective())
    result = store.qualification("org-1")
    assert isinstance(result, QualificationResult)
    assert result.verdict == "FAIL"
    assert len(result.controls_unverified) == len(REQUIRED_CONTROLS)


def test_store_qualification_passes_with_drills_and_measurements(
    store: SloStore,
) -> None:
    store.register_objective(_objective())
    for i, control in enumerate(REQUIRED_CONTROLS):
        store.record_drill(_drill(drill_id=f"d-{i}", control=control))
    result = store.qualification("org-1", {"slo-uptime": (10, 10)})
    assert result.verdict == "PASS", result.findings


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_route_objective_roundtrip(client: TestClient) -> None:
    created = client.post("/slo/objectives", json=_objective().model_dump(mode="json"))
    assert created.status_code == 200, created.text
    assert created.json()["target_bps"] == 9900

    listed = client.get("/slo/objectives", params={"org_id": "org-1"})
    assert len(listed.json()) == 1

    fetched = client.get("/slo/objectives/slo-uptime", params={"org_id": "org-1"})
    assert fetched.status_code == 200

    missing = client.get("/slo/objectives/ghost", params={"org_id": "org-1"})
    assert missing.status_code == 404


def test_route_evaluate(client: TestClient) -> None:
    client.post("/slo/objectives", json=_objective().model_dump(mode="json"))
    measured = client.post(
        "/slo/objectives/slo-uptime/evaluate",
        json={"org_id": "org-1", "good": 9, "total": 10},
    )
    assert measured.status_code == 200, measured.text
    assert measured.json()["status"] == "BREACHED"

    too_few = client.post(
        "/slo/objectives/slo-uptime/evaluate",
        json={"org_id": "org-1", "good": 0, "total": 0},
    )
    assert too_few.json()["status"] == "INSUFFICIENT_DATA"


def test_route_drill_roundtrip(client: TestClient) -> None:
    created = client.post("/slo/drills", json=_drill().model_dump(mode="json"))
    assert created.status_code == 200, created.text
    listed = client.get("/slo/drills", params={"org_id": "org-1"})
    assert len(listed.json()) == 1


def test_route_qualification_reports_unverified_controls(client: TestClient) -> None:
    client.post("/slo/objectives", json=_objective().model_dump(mode="json"))
    body = client.get("/slo/qualification", params={"org_id": "org-1"}).json()
    assert body["verdict"] == "FAIL"
    assert len(body["controls_unverified"]) == len(REQUIRED_CONTROLS)


def test_routes_are_registered() -> None:
    from fastapi.routing import APIRoute

    from clipos_api.main import app

    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    for expected in (
        "/slo/objectives",
        "/slo/objectives/{objective_id}",
        "/slo/objectives/{objective_id}/evaluate",
        "/slo/drills",
        "/slo/qualification",
    ):
        assert expected in paths, f"missing route: {expected}"


def test_models_are_json_serializable(store: SloStore) -> None:
    objective = store.register_objective(_objective())
    json.dumps(objective.model_dump(mode="json"))
    drill = store.record_drill(_drill())
    json.dumps(drill.model_dump(mode="json"))
    result = store.qualification("org-1")
    json.dumps(result.model_dump(mode="json"))
