"""Tests for deterministic assignment/variant orchestration (CLIP-067)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.analytics import MetricKind
from clipos_api.experiment_orchestration import (
    AssignRequest,
    ExperimentVariant,
    OrchestrationStore,
    RegisterVariantRequest,
    deterministic_bucket,
    select_variant,
)
from clipos_api.experiments import (
    ExperimentStore,
    RegisterExperimentRequest,
    TransitionRequest,
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
    path = Path("/tmp") / "clipos-067-orchestration-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> OrchestrationStore:
    return OrchestrationStore.from_engine(engine)


@pytest.fixture()
def experiment_store(engine: Engine) -> ExperimentStore:
    return ExperimentStore.from_engine(engine)


def _register_active_experiment(experiment_store: ExperimentStore) -> None:
    experiment_store.register_experiment(
        RegisterExperimentRequest(
            org_id="org-1",
            experiment_id="exp-1",
            title="Hook test",
            target_metric=MetricKind.VIEW,
            hypothesis_summary="Short hooks win.",
        )
    )
    experiment_store.transition_experiment(
        TransitionRequest(status="ACTIVE", actor_ref="op-1"), "org-1", "exp-1"
    )


def _setup_active_experiment_with_variants(
    experiment_store: ExperimentStore,
    orchestration: OrchestrationStore,
) -> None:
    """Variants register only in DRAFT, so register then activate."""
    experiment_store.register_experiment(
        RegisterExperimentRequest(
            org_id="org-1",
            experiment_id="exp-1",
            title="Hook test",
            target_metric=MetricKind.VIEW,
            hypothesis_summary="Short hooks win.",
        )
    )
    orchestration.register_variant("exp-1", _variant("control"))
    orchestration.register_variant("exp-1", _variant("treatment"))
    experiment_store.transition_experiment(
        TransitionRequest(status="ACTIVE", actor_ref="op-1"), "org-1", "exp-1"
    )


def _variant(key: str, weight: int = 50, label: str | None = None) -> RegisterVariantRequest:
    return RegisterVariantRequest(
        org_id="org-1",
        variant_key=key,
        label=label or f"Variant {key}",
        weight=weight,
    )


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def test_deterministic_bucket_stable() -> None:
    first = deterministic_bucket("org-1", "exp-1", "subject-a", 100)
    second = deterministic_bucket("org-1", "exp-1", "subject-a", 100)
    assert first == second
    assert 0 <= first < 100


def test_deterministic_bucket_varies_by_input() -> None:
    a = deterministic_bucket("org-1", "exp-1", "subject-a", 1_000_000)
    b = deterministic_bucket("org-1", "exp-1", "subject-b", 1_000_000)
    assert a != b


def test_select_variant_deterministic() -> None:
    variants = [
        ExperimentVariant(
            org_id="org-1",
            experiment_id="exp-1",
            variant_key="control",
            label="A",
            weight=50,
        ),
        ExperimentVariant(
            org_id="org-1",
            experiment_id="exp-1",
            variant_key="treatment",
            label="B",
            weight=50,
        ),
    ]
    first = select_variant(variants, "org-1", "exp-1", "subject-x")
    second = select_variant(variants, "org-1", "exp-1", "subject-x")
    assert first == second
    assert first in ("control", "treatment")


def test_select_variant_uses_weight_ranges() -> None:
    # One subject maps to exactly one valid variant regardless of weights.
    control = ExperimentVariant(
        org_id="org-1",
        experiment_id="exp-1",
        variant_key="control",
        label="A",
        weight=90,
    )
    treatment = ExperimentVariant(
        org_id="org-1",
        experiment_id="exp-1",
        variant_key="treatment",
        label="B",
        weight=10,
    )
    for i in range(50):
        chosen = select_variant([control, treatment], "org-1", "exp-1", f"subject-{i}")
        assert chosen in ("control", "treatment")
    # Heavier variant covers most of the bucket space: sanity, not exact.
    counts: dict[str, int] = {"control": 0, "treatment": 0}
    for i in range(500):
        counts[select_variant([control, treatment], "org-1", "exp-1", f"subject-{i}")] += 1
    assert counts["control"] > counts["treatment"]


def test_select_variant_requires_variants() -> None:
    with pytest.raises(ValueError, match="no variants"):
        select_variant([], "org-1", "exp-1", "subject-a")


# ---------------------------------------------------------------------------
# Store: variants
# ---------------------------------------------------------------------------


def test_register_variant_idempotent_conflict(
    store: OrchestrationStore,
    experiment_store: ExperimentStore,
) -> None:
    _register_active_experiment(experiment_store)
    # Variants must be registered in DRAFT; this experiment is ACTIVE.
    with pytest.raises(ValueError, match="only be registered in DRAFT"):
        store.register_variant("exp-1", _variant("control"))


def test_register_variant_in_draft_and_freeze(
    store: OrchestrationStore,
    experiment_store: ExperimentStore,
    audit_log: Path,
) -> None:
    experiment_store.register_experiment(
        RegisterExperimentRequest(
            org_id="org-1",
            experiment_id="exp-2",
            title="Draft test",
            target_metric=MetricKind.LIKE,
            hypothesis_summary="H",
        )
    )
    first = store.register_variant("exp-2", _variant("control"))
    again = store.register_variant("exp-2", _variant("control"))
    assert first.variant_key == again.variant_key
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_variant("exp-2", _variant("control", weight=60))
    assert "experiment_variants.registered" in audit_log.read_text(encoding="utf-8")

    experiment_store.transition_experiment(
        TransitionRequest(status="ACTIVE", actor_ref="op-1"), "org-1", "exp-2"
    )
    with pytest.raises(ValueError, match="only be registered in DRAFT"):
        store.register_variant("exp-2", _variant("treatment"))


def test_register_variant_unknown_experiment(store: OrchestrationStore) -> None:
    with pytest.raises(ValueError, match="experiment not found"):
        store.register_variant("missing", _variant("control"))


# ---------------------------------------------------------------------------
# Store: assignments
# ---------------------------------------------------------------------------


def test_assign_requires_active(
    store: OrchestrationStore,
    experiment_store: ExperimentStore,
) -> None:
    experiment_store.register_experiment(
        RegisterExperimentRequest(
            org_id="org-1",
            experiment_id="exp-draft",
            title="Draft",
            target_metric=MetricKind.VIEW,
            hypothesis_summary="H",
        )
    )
    store.register_variant("exp-draft", _variant("control"))
    store.register_variant("exp-draft", _variant("treatment"))
    with pytest.raises(ValueError, match="must be ACTIVE"):
        store.assign("exp-draft", AssignRequest(subject_ref="subject-1"), "org-1")


def test_assign_requires_two_variants(
    store: OrchestrationStore,
    experiment_store: ExperimentStore,
) -> None:
    experiment_store.register_experiment(
        RegisterExperimentRequest(
            org_id="org-1",
            experiment_id="exp-one",
            title="One variant",
            target_metric=MetricKind.VIEW,
            hypothesis_summary="H",
        )
    )
    store.register_variant(
        "exp-one",
        RegisterVariantRequest(org_id="org-1", variant_key="only", label="Only", weight=100),
    )
    experiment_store.transition_experiment(
        TransitionRequest(status="ACTIVE", actor_ref="op-1"), "org-1", "exp-one"
    )
    with pytest.raises(ValueError, match="at least two variants"):
        store.assign("exp-one", AssignRequest(subject_ref="s1"), "org-1")


def test_assign_deterministic_idempotent_and_stable(
    store: OrchestrationStore,
    experiment_store: ExperimentStore,
    audit_log: Path,
) -> None:
    _setup_active_experiment_with_variants(experiment_store, store)

    first = store.assign("exp-1", AssignRequest(subject_ref="subject-a"), "org-1")
    repeat = store.assign("exp-1", AssignRequest(subject_ref="subject-a"), "org-1")
    assert first.assignment_id == repeat.assignment_id
    assert first.variant_key == repeat.variant_key
    assert "experiment_assignments.assigned" in audit_log.read_text(encoding="utf-8")

    # Persisted assignments survive weight changes: re-registration is
    # frozen post-DRAFT, so simulate stability by checking the stored
    # row wins over any recomputation.
    stored = store.list_assignments("org-1", "exp-1")
    assert len(stored) == 1
    assert stored[0].variant_key == first.variant_key


def test_assign_distribution_covers_both_variants(
    store: OrchestrationStore,
    experiment_store: ExperimentStore,
) -> None:
    _setup_active_experiment_with_variants(experiment_store, store)
    for i in range(40):
        store.assign("exp-1", AssignRequest(subject_ref=f"subject-{i}"), "org-1")
    summary = store.variant_summary("org-1", "exp-1")
    assert summary.total_assigned == 40
    assert set(summary.counts) == {"control", "treatment"}
    assert sum(summary.counts.values()) == 40


def test_assign_unknown_experiment(store: OrchestrationStore) -> None:
    with pytest.raises(ValueError, match="experiment not found"):
        store.assign("missing", AssignRequest(subject_ref="s1"), "org-1")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/experiments/{experiment_id}/variants" in paths
    assert "/experiments/{experiment_id}/assignments" in paths
    assert "/experiments/{experiment_id}/variant-summary" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _setup_draft(client) -> None:
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


def test_variant_and_assignment_route_roundtrip(client) -> None:
    _setup_draft(client)
    for key in ("control", "treatment"):
        resp = client.post(
            "/experiments/exp-1/variants",
            json={"org_id": "org-1", "variant_key": key, "label": key, "weight": 50},
        )
        assert resp.status_code == 200, resp.text

    listing = client.get("/experiments/exp-1/variants", params={"org_id": "org-1"})
    assert len(listing.json()) == 2

    client.post(
        "/experiments/exp-1/transition",
        params={"org_id": "org-1"},
        json={"status": "ACTIVE", "actor_ref": "op-1"},
    )

    assigned = client.post(
        "/experiments/exp-1/assignments",
        params={"org_id": "org-1"},
        json={"subject_ref": "subject-a"},
    )
    assert assigned.status_code == 200, assigned.text
    body = assigned.json()
    assert body["variant_key"] in ("control", "treatment")

    repeat = client.post(
        "/experiments/exp-1/assignments",
        params={"org_id": "org-1"},
        json={"subject_ref": "subject-a"},
    )
    assert repeat.json()["assignment_id"] == body["assignment_id"]

    assignments = client.get("/experiments/exp-1/assignments", params={"org_id": "org-1"})
    assert len(assignments.json()) == 1

    summary = client.get("/experiments/exp-1/variant-summary", params={"org_id": "org-1"})
    assert summary.status_code == 200
    assert summary.json()["total_assigned"] == 1


def test_assign_before_active_409(client) -> None:
    _setup_draft(client)
    for key in ("control", "treatment"):
        client.post(
            "/experiments/exp-1/variants",
            json={"org_id": "org-1", "variant_key": key, "label": key, "weight": 50},
        )
    resp = client.post(
        "/experiments/exp-1/assignments",
        params={"org_id": "org-1"},
        json={"subject_ref": "s1"},
    )
    assert resp.status_code == 409


def test_variant_register_after_active_409(client) -> None:
    _setup_draft(client)
    for key in ("control", "treatment"):
        client.post(
            "/experiments/exp-1/variants",
            json={"org_id": "org-1", "variant_key": key, "label": key, "weight": 50},
        )
    client.post(
        "/experiments/exp-1/transition",
        params={"org_id": "org-1"},
        json={"status": "ACTIVE", "actor_ref": "op-1"},
    )
    resp = client.post(
        "/experiments/exp-1/variants",
        json={"org_id": "org-1", "variant_key": "extra", "label": "x", "weight": 10},
    )
    assert resp.status_code == 409


def test_unknown_experiment_404(client) -> None:
    resp = client.post(
        "/experiments/missing/variants",
        json={"org_id": "org-1", "variant_key": "control", "label": "c", "weight": 50},
    )
    assert resp.status_code == 404
