"""Tests for experiment/hypothesis data model (CLIP-066)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.analytics import MetricKind
from clipos_api.experiments import (
    ExperimentStatus,
    ExperimentStore,
    HypothesisDirection,
    HypothesisStatus,
    RegisterExperimentRequest,
    RegisterHypothesisRequest,
    TransitionRequest,
    experiment_transition_allowed,
    hypothesis_transition_allowed,
)
from clipos_api.main import app


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-066-experiments-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> ExperimentStore:
    return ExperimentStore.from_engine(engine)


def _experiment(**overrides: object) -> RegisterExperimentRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "experiment_id": "exp-1",
        "title": "Hook style A vs B",
        "target_metric": MetricKind.VIEW,
        "hypothesis_summary": "Shorter hooks raise views.",
    }
    payload.update(overrides)
    return RegisterExperimentRequest(**payload)  # type: ignore[arg-type]


def _hypothesis(**overrides: object) -> RegisterHypothesisRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "hypothesis_id": "hyp-1",
        "experiment_id": "exp-1",
        "statement": "Shorter hooks increase views by 20%.",
        "direction": HypothesisDirection.INCREASE,
    }
    payload.update(overrides)
    return RegisterHypothesisRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure transition tables
# ---------------------------------------------------------------------------


def test_experiment_lifecycle_happy_path() -> None:
    assert experiment_transition_allowed(ExperimentStatus.DRAFT, ExperimentStatus.ACTIVE)
    assert experiment_transition_allowed(ExperimentStatus.ACTIVE, ExperimentStatus.COMPLETED)
    assert experiment_transition_allowed(ExperimentStatus.COMPLETED, ExperimentStatus.ARCHIVED)
    assert experiment_transition_allowed(ExperimentStatus.DRAFT, ExperimentStatus.ARCHIVED)


def test_experiment_illegal_moves() -> None:
    assert not experiment_transition_allowed(ExperimentStatus.DRAFT, ExperimentStatus.COMPLETED)
    assert not experiment_transition_allowed(ExperimentStatus.ARCHIVED, ExperimentStatus.ACTIVE)
    assert not experiment_transition_allowed(ExperimentStatus.COMPLETED, ExperimentStatus.ACTIVE)


def test_experiment_same_state_is_noop_allowed() -> None:
    assert experiment_transition_allowed(ExperimentStatus.ACTIVE, ExperimentStatus.ACTIVE)


def test_hypothesis_transitions() -> None:
    assert hypothesis_transition_allowed(HypothesisStatus.PROPOSED, HypothesisStatus.SUPPORTED)
    assert hypothesis_transition_allowed(HypothesisStatus.PROPOSED, HypothesisStatus.REJECTED)
    assert not hypothesis_transition_allowed(HypothesisStatus.SUPPORTED, HypothesisStatus.REJECTED)
    assert not hypothesis_transition_allowed(HypothesisStatus.REJECTED, HypothesisStatus.SUPPORTED)


# ---------------------------------------------------------------------------
# Experiment store
# ---------------------------------------------------------------------------


def test_register_experiment_idempotent(store: ExperimentStore) -> None:
    first = store.register_experiment(_experiment())
    second = store.register_experiment(_experiment())
    assert first.experiment_id == second.experiment_id
    assert first.status is ExperimentStatus.DRAFT
    assert len(store.list_experiments("org-1")) == 1


def test_register_experiment_conflict(store: ExperimentStore) -> None:
    store.register_experiment(_experiment())
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_experiment(_experiment(title="Changed title"))


def test_register_experiment_audit(
    store: ExperimentStore,
    audit_log: Path,
) -> None:
    store.register_experiment(_experiment())
    assert "experiments.registered" in audit_log.read_text(encoding="utf-8")


def test_transition_experiment_flow(
    store: ExperimentStore,
    audit_log: Path,
) -> None:
    store.register_experiment(_experiment())
    move = TransitionRequest(status="ACTIVE", actor_ref="op-1")
    active = store.transition_experiment(move, "org-1", "exp-1")
    assert active.status is ExperimentStatus.ACTIVE
    done = store.transition_experiment(
        TransitionRequest(status="COMPLETED", actor_ref="op-1"), "org-1", "exp-1"
    )
    assert done.status is ExperimentStatus.COMPLETED
    assert "experiments.status_changed" in audit_log.read_text(encoding="utf-8")


def test_transition_experiment_illegal(store: ExperimentStore) -> None:
    store.register_experiment(_experiment())
    with pytest.raises(ValueError, match="illegal experiment transition"):
        store.transition_experiment(
            TransitionRequest(status="COMPLETED", actor_ref="op-1"),
            "org-1",
            "exp-1",
        )


def test_transition_experiment_same_state_noop(store: ExperimentStore) -> None:
    store.register_experiment(_experiment())
    again = store.transition_experiment(
        TransitionRequest(status="DRAFT", actor_ref="op-1"), "org-1", "exp-1"
    )
    assert again.status is ExperimentStatus.DRAFT


def test_list_experiments_status_filter(store: ExperimentStore) -> None:
    store.register_experiment(_experiment())
    store.register_experiment(_experiment(experiment_id="exp-2"))
    store.transition_experiment(
        TransitionRequest(status="ACTIVE", actor_ref="op-1"), "org-1", "exp-2"
    )
    assert len(store.list_experiments("org-1")) == 2
    assert len(store.list_experiments("org-1", ExperimentStatus.DRAFT)) == 1
    assert len(store.list_experiments("org-1", ExperimentStatus.ACTIVE)) == 1
    assert len(store.list_experiments("org-other")) == 0


# ---------------------------------------------------------------------------
# Hypothesis store
# ---------------------------------------------------------------------------


def test_register_hypothesis_requires_experiment(store: ExperimentStore) -> None:
    with pytest.raises(ValueError, match="experiment not found"):
        store.register_hypothesis(_hypothesis())


def test_register_hypothesis_idempotent_conflict(store: ExperimentStore) -> None:
    store.register_experiment(_experiment())
    first = store.register_hypothesis(_hypothesis())
    second = store.register_hypothesis(_hypothesis())
    assert first.hypothesis_id == second.hypothesis_id
    assert first.status is HypothesisStatus.PROPOSED
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_hypothesis(_hypothesis(statement="Different."))


def test_hypothesis_resolution_and_freeze(store: ExperimentStore) -> None:
    store.register_experiment(_experiment())
    store.register_hypothesis(_hypothesis())
    supported = store.transition_hypothesis(
        TransitionRequest(status="SUPPORTED", actor_ref="op-1", reason_ref="eval-9"),
        "org-1",
        "hyp-1",
    )
    assert supported.status is HypothesisStatus.SUPPORTED
    with pytest.raises(ValueError, match="illegal hypothesis transition"):
        store.transition_hypothesis(
            TransitionRequest(status="REJECTED", actor_ref="op-1"), "org-1", "hyp-1"
        )


def test_list_hypotheses_experiment_filter(store: ExperimentStore) -> None:
    store.register_experiment(_experiment())
    store.register_experiment(_experiment(experiment_id="exp-2"))
    store.register_hypothesis(_hypothesis())
    store.register_hypothesis(_hypothesis(hypothesis_id="hyp-2", experiment_id="exp-2"))
    assert len(store.list_hypotheses("org-1")) == 2
    assert len(store.list_hypotheses("org-1", "exp-1")) == 1
    assert len(store.list_hypotheses("org-1", "exp-2")) == 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/experiments" in paths
    assert "/experiments/{experiment_id}" in paths
    assert "/experiments/{experiment_id}/transition" in paths
    assert "/hypotheses" in paths
    assert "/hypotheses/{hypothesis_id}/transition" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_experiment_route_roundtrip(client) -> None:
    payload = {
        "org_id": "org-1",
        "experiment_id": "exp-1",
        "title": "Hook style A vs B",
        "target_metric": "VIEW",
        "hypothesis_summary": "Shorter hooks raise views.",
    }
    created = client.post("/experiments", json=payload)
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "DRAFT"

    fetched = client.get("/experiments/exp-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200

    missing = client.get("/experiments/nope", params={"org_id": "org-1"})
    assert missing.status_code == 404

    conflict = client.post("/experiments", json={**payload, "title": "Changed"})
    assert conflict.status_code == 409

    moved = client.post(
        "/experiments/exp-1/transition",
        params={"org_id": "org-1"},
        json={"status": "ACTIVE", "actor_ref": "op-1"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == "ACTIVE"

    illegal = client.post(
        "/experiments/exp-1/transition",
        params={"org_id": "org-1"},
        json={"status": "ARCHIVED", "actor_ref": "op-1"},
    )
    # ACTIVE -> ARCHIVED is illegal (only ACTIVE -> COMPLETED).
    assert illegal.status_code == 409


def test_hypothesis_route_roundtrip(client) -> None:
    client.post(
        "/experiments",
        json={
            "org_id": "org-1",
            "experiment_id": "exp-1",
            "title": "T",
            "target_metric": "VIEW",
            "hypothesis_summary": "H",
        },
    )
    created = client.post(
        "/hypotheses",
        json={
            "org_id": "org-1",
            "hypothesis_id": "hyp-1",
            "experiment_id": "exp-1",
            "statement": "Shorter hooks increase views.",
            "direction": "INCREASE",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "PROPOSED"

    orphan = client.post(
        "/hypotheses",
        json={
            "org_id": "org-1",
            "hypothesis_id": "hyp-2",
            "experiment_id": "missing-exp",
            "statement": "S",
            "direction": "DECREASE",
        },
    )
    assert orphan.status_code == 404

    resolved = client.post(
        "/hypotheses/hyp-1/transition",
        params={"org_id": "org-1"},
        json={"status": "SUPPORTED", "actor_ref": "op-1", "reason_ref": "eval-1"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "SUPPORTED"

    listing = client.get("/hypotheses", params={"org_id": "org-1", "experiment_id": "exp-1"})
    assert len(listing.json()) == 1


def test_invalid_slug_rejected(client) -> None:
    resp = client.post(
        "/experiments",
        json={
            "org_id": "org-1",
            "experiment_id": "bad/id",
            "title": "T",
            "target_metric": "VIEW",
            "hypothesis_summary": "H",
        },
    )
    assert resp.status_code == 422
