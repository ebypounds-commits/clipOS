"""Tests for result evaluation/business significance (CLIP-068)."""

from __future__ import annotations

from math import erfc, sqrt
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.analytics import MetricKind
from clipos_api.experiment_evaluations import (
    EvaluateRequest,
    EvaluationStore,
    VariantStats,
    evaluate_significance,
)
from clipos_api.experiments import ExperimentStore, RegisterExperimentRequest
from clipos_api.main import app


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-068-evaluations-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> EvaluationStore:
    return EvaluationStore.from_engine(engine)


@pytest.fixture()
def experiment_store(engine: Engine) -> ExperimentStore:
    return ExperimentStore.from_engine(engine)


def _ensure_experiment(experiment_store: ExperimentStore) -> None:
    experiment_store.register_experiment(
        RegisterExperimentRequest(
            org_id="org-1",
            experiment_id="exp-1",
            title="Hook test",
            target_metric=MetricKind.VIEW,
            hypothesis_summary="Short hooks win.",
        )
    )


def _stats(key: str, n: int, mean: float, stddev: float) -> VariantStats:
    return VariantStats(variant_key=key, sample_count=n, mean=mean, stddev=stddev)


def _request(**overrides: object) -> EvaluateRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "eval_id": "eval-1",
        "variant_a": _stats("control", 100, 10.0, 5.0),
        "variant_b": _stats("treatment", 100, 12.0, 5.0),
        "actor_ref": "op-1",
    }
    payload.update(overrides)
    return EvaluateRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure significance math
# ---------------------------------------------------------------------------


def test_equal_means_never_significant() -> None:
    result = evaluate_significance(_stats("a", 50, 8.0, 2.0), _stats("b", 50, 8.0, 2.0))
    assert result.lift_pct == 0.0
    assert result.z_score == 0.0
    assert result.p_value == 1.0
    assert result.statistically_significant is False
    assert result.business_significant is False
    assert result.winner_variant_key is None


def test_clear_winner_known_values() -> None:
    result = evaluate_significance(
        _stats("control", 100, 10.0, 5.0),
        _stats("treatment", 100, 12.0, 5.0),
    )
    # se = sqrt(25/100 + 25/100) = sqrt(0.5); z = 2 / sqrt(0.5).
    expected_z = 2.0 / sqrt(0.5)
    assert result.z_score == pytest.approx(expected_z)
    assert result.p_value == pytest.approx(erfc(abs(expected_z) / sqrt(2.0)))
    assert result.lift_pct == pytest.approx(20.0)
    assert result.statistically_significant is True
    assert result.business_significant is True
    assert result.winner_variant_key == "treatment"


def test_significant_but_business_too_small() -> None:
    result = evaluate_significance(
        _stats("control", 1000, 100.0, 5.0),
        _stats("treatment", 1000, 100.5, 5.0),
        min_effect_pct=5.0,
    )
    assert result.statistically_significant is True
    assert result.business_significant is False
    assert result.winner_variant_key is None
    assert abs(result.lift_pct) < 5.0


def test_noisy_data_not_significant() -> None:
    result = evaluate_significance(
        _stats("control", 30, 10.0, 20.0),
        _stats("treatment", 30, 12.0, 20.0),
    )
    assert result.statistically_significant is False
    assert result.business_significant is False
    assert result.winner_variant_key is None


def test_zero_stddev_different_means() -> None:
    result = evaluate_significance(
        _stats("control", 40, 10.0, 0.0),
        _stats("treatment", 40, 11.0, 0.0),
    )
    assert result.p_value == 0.0
    assert result.statistically_significant is True
    assert result.winner_variant_key == "treatment"


def test_regressor_declares_variant_a_winner() -> None:
    result = evaluate_significance(
        _stats("control", 100, 12.0, 5.0),
        _stats("treatment", 100, 10.0, 5.0),
    )
    assert result.statistically_significant is True
    assert result.business_significant is True
    assert result.winner_variant_key == "control"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_evaluate_requires_experiment(store: EvaluationStore) -> None:
    with pytest.raises(ValueError, match="experiment not found"):
        store.evaluate("missing", _request())


def test_evaluate_persists_and_audits(
    store: EvaluationStore,
    experiment_store: ExperimentStore,
    audit_log: Path,
) -> None:
    _ensure_experiment(experiment_store)
    result = store.evaluate("exp-1", _request())
    assert result.eval_id == "eval-1"
    assert result.winner_variant_key == "treatment"
    assert "experiment_evaluations.recorded" in audit_log.read_text(encoding="utf-8")

    stored = store.list_evaluations("org-1", "exp-1")
    assert len(stored) == 1
    assert stored[0].lift_pct == pytest.approx(20.0)


def test_evaluate_idempotent_conflict(
    store: EvaluationStore,
    experiment_store: ExperimentStore,
) -> None:
    _ensure_experiment(experiment_store)
    first = store.evaluate("exp-1", _request())
    repeat = store.evaluate("exp-1", _request())
    assert first.eval_id == repeat.eval_id
    assert len(store.list_evaluations("org-1", "exp-1")) == 1
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.evaluate("exp-1", _request(variant_b=_stats("treatment", 100, 99.0, 5.0)))


def test_list_evaluations_org_scoped(
    store: EvaluationStore,
    experiment_store: ExperimentStore,
) -> None:
    _ensure_experiment(experiment_store)
    store.evaluate("exp-1", _request())
    assert len(store.list_evaluations("org-other", "exp-1")) == 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/experiments/{experiment_id}/evaluate" in paths
    assert "/experiments/{experiment_id}/evaluations" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "eval_id": "eval-1",
        "variant_a": {
            "variant_key": "control",
            "sample_count": 100,
            "mean": 10.0,
            "stddev": 5.0,
        },
        "variant_b": {
            "variant_key": "treatment",
            "sample_count": 100,
            "mean": 12.0,
            "stddev": 5.0,
        },
        "actor_ref": "op-1",
    }


def _setup_experiment(client) -> None:
    client.post(
        "/experiments",
        json={
            "org_id": "org-1",
            "experiment_id": "exp-1",
            "title": "Hook test",
            "target_metric": "VIEW",
            "hypothesis_summary": "Short hooks win.",
        },
    )


def test_evaluate_route_roundtrip(client) -> None:
    _setup_experiment(client)
    resp = client.post("/experiments/exp-1/evaluate", json=_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["winner_variant_key"] == "treatment"
    assert body["business_significant"] is True

    listing = client.get("/experiments/exp-1/evaluations", params={"org_id": "org-1"})
    assert listing.status_code == 200
    assert len(listing.json()) == 1

    repeat = client.post("/experiments/exp-1/evaluate", json=_payload())
    assert repeat.status_code == 200
    assert repeat.json()["eval_id"] == "eval-1"


def test_evaluate_conflict_409(client) -> None:
    _setup_experiment(client)
    client.post("/experiments/exp-1/evaluate", json=_payload())
    changed = _payload()
    changed["variant_b"] = {
        "variant_key": "treatment",
        "sample_count": 100,
        "mean": 99.0,
        "stddev": 5.0,
    }
    resp = client.post("/experiments/exp-1/evaluate", json=changed)
    assert resp.status_code == 409


def test_evaluate_unknown_experiment_404(client) -> None:
    resp = client.post("/experiments/missing/evaluate", json=_payload())
    assert resp.status_code == 404


def test_evaluate_invalid_eval_id_422(client) -> None:
    _setup_experiment(client)
    payload = _payload()
    payload["eval_id"] = "bad/id"
    resp = client.post("/experiments/exp-1/evaluate", json=payload)
    assert resp.status_code == 422
