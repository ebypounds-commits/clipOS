"""Tests for CI/CD canary rollout and rollback (CLIP-095)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.deployments import (
    CanaryRequest,
    DeploymentStatus,
    DeploymentStore,
    Environment,
    PromoteRequest,
    RecordEvidenceRequest,
    RegisterDeploymentRequest,
    RollbackRequest,
    Strategy,
    deployment_transition_allowed,
    health_window_passed,
    rollback_conflicts,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-095-deployments-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> DeploymentStore:
    return DeploymentStore.from_engine(engine)


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _register(**overrides: object) -> RegisterDeploymentRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "deployment_id": "dep-2",
        "environment": Environment.PRODUCTION,
        "strategy": Strategy.CANARY,
        "commit_sha": "abcdef1234567890",
        "image_digest": "sha256:" + "a" * 64,
        "migrations": ["0069"],
        "parent_deployment_id": None,
    }
    payload.update(overrides)
    return RegisterDeploymentRequest(**payload)  # type: ignore[arg-type]


def _seed_parent(store: DeploymentStore, migrations: list[str]) -> None:
    store.register_deployment(
        _register(
            deployment_id="dep-1",
            strategy=Strategy.FULL,
            commit_sha="1111111111111111",
            image_digest="sha256:" + "b" * 64,
            migrations=migrations,
            parent_deployment_id=None,
        )
    )


# ---------------------------------------------------------------------------
# Pure predicates
# ---------------------------------------------------------------------------


def test_transition_table_allows_the_documented_paths() -> None:
    assert deployment_transition_allowed(DeploymentStatus.PENDING, DeploymentStatus.CANARY)
    assert deployment_transition_allowed(DeploymentStatus.CANARY, DeploymentStatus.PROMOTED)
    assert deployment_transition_allowed(DeploymentStatus.CANARY, DeploymentStatus.ROLLED_BACK)
    assert deployment_transition_allowed(DeploymentStatus.FAILED, DeploymentStatus.ROLLED_BACK)
    assert deployment_transition_allowed(DeploymentStatus.PROMOTED, DeploymentStatus.PROMOTED)


def test_transition_table_refuses_the_forbidden_paths() -> None:
    assert not deployment_transition_allowed(
        DeploymentStatus.ROLLED_BACK, DeploymentStatus.PROMOTED
    )
    assert not deployment_transition_allowed(DeploymentStatus.PENDING, DeploymentStatus.ROLLED_BACK)
    assert not deployment_transition_allowed(DeploymentStatus.PROMOTED, DeploymentStatus.CANARY)


def test_health_window_requires_non_empty_all_healthy() -> None:
    assert health_window_passed(["HEALTHY", "HEALTHY"])
    assert not health_window_passed([])
    assert not health_window_passed(["HEALTHY", "DEGRADED"])
    assert not health_window_passed(["DOWN"])


def test_rollback_conflicts_reports_migrations_the_target_lacks() -> None:
    assert rollback_conflicts(["0068", "0069"], ["0068"]) == ["0069"]
    assert rollback_conflicts(["0068"], ["0068", "0069"]) == []
    assert rollback_conflicts([], []) == []


# ---------------------------------------------------------------------------
# Store: registration and rollout
# ---------------------------------------------------------------------------


def test_register_is_idempotent(store: DeploymentStore, audit_log: Path) -> None:
    first = store.register_deployment(_register())
    second = store.register_deployment(_register())
    assert first == second
    assert first.status is DeploymentStatus.CANARY
    assert "deployments.registered" in audit_log.read_text(encoding="utf-8")


def test_register_is_idempotent_but_rejects_field_drift(store: DeploymentStore) -> None:
    store.register_deployment(_register())
    with pytest.raises(ValueError, match="different fields"):
        store.register_deployment(_register(commit_sha="9999999999999999"))


def test_full_strategy_registers_as_promoted(store: DeploymentStore) -> None:
    stored = store.register_deployment(
        _register(deployment_id="dep-full", strategy=Strategy.FULL, parent_deployment_id=None)
    )
    assert stored.status is DeploymentStatus.PROMOTED


def test_register_requires_the_parent_to_exist_when_named(store: DeploymentStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.register_deployment(_register(parent_deployment_id="nope"))


def test_register_accepts_a_known_parent(store: DeploymentStore) -> None:
    _seed_parent(store, ["0068"])
    stored = store.register_deployment(_register(parent_deployment_id="dep-1"))
    assert stored.parent_deployment_id == "dep-1"


def test_promote_requires_a_passing_health_window(store: DeploymentStore) -> None:
    _seed_parent(store, ["0068"])
    store.register_deployment(_register())
    with pytest.raises(ValueError, match="has not passed"):
        store.promote("dep-2", PromoteRequest(org_id="org-1", actor_ref="op-1"))

    promoted = store.promote(
        "dep-2",
        PromoteRequest(
            org_id="org-1", actor_ref="op-1", health_observations=["HEALTHY", "HEALTHY"]
        ),
    )
    assert promoted.status is DeploymentStatus.PROMOTED


def test_promote_is_idempotent_once_promoted(store: DeploymentStore) -> None:
    _seed_parent(store, ["0068"])
    store.register_deployment(_register(strategy=Strategy.FULL))
    promoted = store.promote(
        "dep-2", PromoteRequest(org_id="org-1", actor_ref="op-1", health_observations=["HEALTHY"])
    )
    again = store.promote("dep-2", PromoteRequest(org_id="org-1", actor_ref="op-1"))
    assert promoted == again


def test_canary_then_fail_then_rollback(store: DeploymentStore, audit_log: Path) -> None:
    _seed_parent(store, ["0068"])
    store.register_deployment(_register(migrations=["0068"]))
    failed = store.fail("dep-2", CanaryRequest(org_id="org-1", actor_ref="op-1"))
    assert failed.status is DeploymentStatus.FAILED

    rolled = store.rollback(
        "dep-2",
        RollbackRequest(
            org_id="org-1", actor_ref="op-1", target_deployment_id="dep-1", reason_ref="inc-1"
        ),
    )
    assert rolled.status is DeploymentStatus.ROLLED_BACK
    log = audit_log.read_text(encoding="utf-8")
    assert "deployments.rolled_back" in log


def test_rollback_conflicts_are_refused(store: DeploymentStore) -> None:
    """Rolling back onto a schema missing our migrations must be refused."""
    _seed_parent(store, ["0068"])
    store.register_deployment(_register(migrations=["0068", "0069"]))
    with pytest.raises(ValueError, match="predates"):
        store.rollback(
            "dep-2",
            RollbackRequest(
                org_id="org-1",
                actor_ref="op-1",
                target_deployment_id="dep-1",
                reason_ref="inc-1",
            ),
        )


def test_rollback_to_self_is_refused(store: DeploymentStore) -> None:
    _seed_parent(store, ["0068"])
    store.register_deployment(_register(migrations=["0068"]))
    with pytest.raises(ValueError, match="current deployment"):
        store.rollback(
            "dep-2",
            RollbackRequest(
                org_id="org-1",
                actor_ref="op-1",
                target_deployment_id="dep-2",
                reason_ref="inc-1",
            ),
        )


def test_rollback_is_terminal(store: DeploymentStore) -> None:
    _seed_parent(store, ["0068"])
    store.register_deployment(_register(migrations=["0068"]))
    store.rollback(
        "dep-2",
        RollbackRequest(
            org_id="org-1", actor_ref="op-1", target_deployment_id="dep-1", reason_ref="inc-1"
        ),
    )
    with pytest.raises(ValueError, match="already rolled back"):
        store.rollback(
            "dep-2",
            RollbackRequest(
                org_id="org-1",
                actor_ref="op-1",
                target_deployment_id="dep-1",
                reason_ref="inc-2",
            ),
        )


def test_rollback_unknown_target_is_refused(store: DeploymentStore) -> None:
    _seed_parent(store, ["0068"])
    store.register_deployment(_register(migrations=["0068"]))
    with pytest.raises(ValueError, match="not found"):
        store.rollback(
            "dep-2",
            RollbackRequest(
                org_id="org-1",
                actor_ref="op-1",
                target_deployment_id="ghost",
                reason_ref="inc-1",
            ),
        )


def test_list_deployments_filters_by_environment(store: DeploymentStore) -> None:
    _seed_parent(store, ["0068"])
    store.register_deployment(_register(deployment_id="dep-stage", environment=Environment.STAGING))
    production = store.list_deployments("org-1", Environment.PRODUCTION)
    assert [d.deployment_id for d in production] == ["dep-1"]
    assert len(store.list_deployments("org-1")) == 2


# ---------------------------------------------------------------------------
# Store: release evidence
# ---------------------------------------------------------------------------


def _evidence(**overrides: object) -> RecordEvidenceRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "actor_ref": "op-1",
        "commit_sha": "abcdef1234567890",
        "image_digest": "sha256:" + "a" * 64,
        "migrations": ["0069"],
        "config_version": "cfg-7",
        "agent_versions": ["writer@1.2.0"],
        "policy_versions": ["publish@3"],
        "ci_runs": ["38785427571"],
        "security_scan_summary": "secret_scan clean; 11/11 controls PASS",
        "rollback_target": "dep-1",
        "operator_approvals": ["approval://op-1"],
    }
    payload.update(overrides)
    return RecordEvidenceRequest(**payload)  # type: ignore[arg-type]


def test_record_evidence_captures_the_spec_fields(store: DeploymentStore, audit_log: Path) -> None:
    _seed_parent(store, ["0068"])
    store.register_deployment(_register(migrations=["0068"]))
    stored = store.record_evidence("dep-2", _evidence())

    assert stored.commit_sha == "abcdef1234567890"
    assert stored.image_digest.startswith("sha256:")
    assert stored.migrations == ["0069"]
    assert stored.config_version == "cfg-7"
    assert stored.agent_versions == ["writer@1.2.0"]
    assert stored.policy_versions == ["publish@3"]
    assert stored.ci_runs == ["38785427571"]
    assert stored.security_scan_summary
    assert stored.rollback_target == "dep-1"
    assert stored.operator_approvals == ["approval://op-1"]
    assert stored.deployed_at is not None
    assert "release_evidence.recorded" in audit_log.read_text(encoding="utf-8")


def test_record_evidence_is_idempotent(store: DeploymentStore) -> None:
    _seed_parent(store, ["0068"])
    store.register_deployment(_register(migrations=["0068"]))
    first = store.record_evidence("dep-2", _evidence())
    second = store.record_evidence("dep-2", _evidence())
    assert first.deployed_at == second.deployed_at


def test_record_evidence_rejects_field_drift(store: DeploymentStore) -> None:
    _seed_parent(store, ["0068"])
    store.register_deployment(_register(migrations=["0068"]))
    store.record_evidence("dep-2", _evidence())
    with pytest.raises(ValueError, match="different fields"):
        store.record_evidence("dep-2", _evidence(config_version="cfg-8"))


def test_record_evidence_requires_a_known_deployment(store: DeploymentStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.record_evidence("ghost", _evidence())


def test_evidence_marks_rolled_back_timestamp(store: DeploymentStore) -> None:
    _seed_parent(store, ["0068"])
    store.register_deployment(_register(migrations=["0068"]))
    store.rollback(
        "dep-2",
        RollbackRequest(
            org_id="org-1", actor_ref="op-1", target_deployment_id="dep-1", reason_ref="inc-1"
        ),
    )
    stored = store.record_evidence("dep-2", _evidence())
    assert stored.rolled_back_at is not None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _register_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "deployment_id": "dep-2",
        "org_id": "org-1",
        "environment": "PRODUCTION",
        "strategy": "CANARY",
        "commit_sha": "abcdef1234567890",
        "image_digest": "sha256:" + "a" * 64,
        "migrations": ["0068"],
        "parent_deployment_id": None,
    }
    payload.update(overrides)
    return payload


def test_route_register_and_get(client: TestClient) -> None:
    created = client.post("/deployments", json=_register_payload())
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "CANARY"

    fetched = client.get("/deployments/dep-2", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert fetched.json()["deployment_id"] == "dep-2"

    missing = client.get("/deployments/ghost", params={"org_id": "org-1"})
    assert missing.status_code == 404


def test_route_promote_requires_health_window(client: TestClient) -> None:
    client.post("/deployments", json=_register_payload())
    blocked = client.post(
        "/deployments/dep-2/promote",
        json={"org_id": "org-1", "actor_ref": "op-1", "health_observations": []},
    )
    assert blocked.status_code == 409

    promoted = client.post(
        "/deployments/dep-2/promote",
        json={"org_id": "org-1", "actor_ref": "op-1", "health_observations": ["HEALTHY"]},
    )
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "PROMOTED"


def test_route_rollback_accepts_subset_migrations(client: TestClient) -> None:
    client.post(
        "/deployments", json=_register_payload(migrations=["0068"], parent_deployment_id=None)
    )
    client.post(
        "/deployments",
        json=_register_payload(deployment_id="dep-1", strategy="FULL", migrations=["0068"]),
    )
    rolled = client.post(
        "/deployments/dep-2/rollback",
        json={
            "org_id": "org-1",
            "actor_ref": "op-1",
            "target_deployment_id": "dep-1",
            "reason_ref": "inc-1",
        },
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["status"] == "ROLLED_BACK"


def test_route_rollback_refuses_migration_conflict(client: TestClient) -> None:
    client.post(
        "/deployments",
        json=_register_payload(deployment_id="dep-1", strategy="FULL", migrations=["0068"]),
    )
    client.post(
        "/deployments",
        json=_register_payload(migrations=["0068", "0069"]),
    )
    refused = client.post(
        "/deployments/dep-2/rollback",
        json={
            "org_id": "org-1",
            "actor_ref": "op-1",
            "target_deployment_id": "dep-1",
            "reason_ref": "inc-1",
        },
    )
    assert refused.status_code == 409
    assert "0069" in refused.json()["detail"]


def test_route_evidence_roundtrip(client: TestClient) -> None:
    client.post("/deployments", json=_register_payload())
    recorded = client.post(
        "/deployments/dep-2/evidence",
        json={
            "org_id": "org-1",
            "actor_ref": "op-1",
            "commit_sha": "abcdef1234567890",
            "image_digest": "sha256:" + "a" * 64,
            "migrations": ["0068"],
            "config_version": "cfg-7",
            "agent_versions": ["writer@1.2.0"],
            "policy_versions": ["publish@3"],
            "ci_runs": ["38785427571"],
            "security_scan_summary": "clean",
            "operator_approvals": ["approval://op-1"],
        },
    )
    assert recorded.status_code == 200, recorded.text

    fetched = client.get("/deployments/dep-2/evidence", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert fetched.json()["config_version"] == "cfg-7"


def test_route_evidence_missing_returns_404(client: TestClient) -> None:
    client.post("/deployments", json=_register_payload())
    assert client.get("/deployments/dep-2/evidence", params={"org_id": "org-1"}).status_code == 404


def test_route_list_filters_by_environment(client: TestClient) -> None:
    client.post("/deployments", json=_register_payload(parent_deployment_id=None))
    client.post(
        "/deployments",
        json=_register_payload(deployment_id="dep-stage", environment="STAGING"),
    )
    production = client.get("/deployments", params={"org_id": "org-1", "environment": "PRODUCTION"})
    assert production.status_code == 200
    assert [d["deployment_id"] for d in production.json()] == ["dep-2"]


def test_routes_are_registered() -> None:
    from fastapi.routing import APIRoute

    from clipos_api.main import app

    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    for expected in (
        "/deployments",
        "/deployments/{deployment_id}",
        "/deployments/{deployment_id}/canary",
        "/deployments/{deployment_id}/promote",
        "/deployments/{deployment_id}/fail",
        "/deployments/{deployment_id}/rollback",
        "/deployments/{deployment_id}/evidence",
    ):
        assert expected in paths, f"missing route: {expected}"


def test_json_serializable_models(store: DeploymentStore) -> None:
    _seed_parent(store, ["0068"])
    deployment = store.register_deployment(_register(migrations=["0068"]))
    json.dumps(deployment.model_dump(mode="json"))
    evidence = store.record_evidence("dep-2", _evidence())
    json.dumps(evidence.model_dump(mode="json"))
