"""Tests for the publishing adapter contract and private-test policy (CLIP-058)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.publishing import (
    AdapterHealth,
    MockPublishingAdapter,
    OperatingMode,
    PublishingStore,
    PublishPolicy,
    PublishStatus,
    PublishSubmission,
    Visibility,
    evaluate_publish_policy,
    submit_publication,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-058-publishing-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> PublishingStore:
    return PublishingStore.from_engine(engine)


def _policy(**overrides: object) -> PublishPolicy:
    payload: dict[str, object] = {
        "policy_id": "policy-1",
        "org_id": "org-1",
        "platform": "youtube",
    }
    payload.update(overrides)
    return PublishPolicy(**payload)  # type: ignore[arg-type]


def _submission(**overrides: object) -> PublishSubmission:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "platform": "youtube",
        "account_ref": "acct-1",
        "content_ref": "storage://videos/clip-1.mp4",
        "title": "My clip",
        "visibility": Visibility.PUBLIC,
        "idempotency_key": "idem-001",
        "operating_mode": OperatingMode.GUARDED_PUBLIC,
    }
    payload.update(overrides)
    return PublishSubmission(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure policy evaluation
# ---------------------------------------------------------------------------


def test_kill_switch_denies_everything() -> None:
    policy = PublishPolicy(
        policy_id="p",
        org_id="org-1",
        platform="youtube",
        public_enabled=True,
        kill_switch=True,
    )
    allowed, reason = evaluate_publish_policy(
        policy, Visibility.PRIVATE, OperatingMode.AUTONOMOUS_PUBLIC
    )
    assert allowed is False
    assert "kill_switch" in reason


def test_observe_and_draft_deny() -> None:
    policy = _policy()
    for mode in (OperatingMode.OBSERVE, OperatingMode.DRAFT):
        allowed, reason = evaluate_publish_policy(policy, Visibility.PRIVATE, mode)
        assert allowed is False
        assert mode.value in reason


def test_private_test_forbids_public() -> None:
    policy = _policy(public_enabled=True)
    allowed, _ = evaluate_publish_policy(policy, Visibility.PUBLIC, OperatingMode.PRIVATE_TEST)
    assert allowed is False


def test_private_test_allows_private_unlisted() -> None:
    policy = _policy()
    for vis in (Visibility.PRIVATE, Visibility.UNLISTED):
        allowed, _ = evaluate_publish_policy(policy, vis, OperatingMode.PRIVATE_TEST)
        assert allowed is True


def test_public_requires_public_enabled() -> None:
    policy = _policy(
        allowed_visibilities=[Visibility.PRIVATE, Visibility.UNLISTED, Visibility.PUBLIC],
        public_enabled=False,
    )
    allowed, _ = evaluate_publish_policy(policy, Visibility.PUBLIC, OperatingMode.GUARDED_PUBLIC)
    assert allowed is False
    policy2 = _policy(
        allowed_visibilities=[Visibility.PRIVATE, Visibility.UNLISTED, Visibility.PUBLIC],
        public_enabled=True,
    )
    allowed2, _ = evaluate_publish_policy(policy2, Visibility.PUBLIC, OperatingMode.GUARDED_PUBLIC)
    assert allowed2 is True


def test_visibility_not_in_allowed_denied() -> None:
    policy = PublishPolicy(
        policy_id="p",
        org_id="org-1",
        platform="youtube",
        allowed_visibilities=[Visibility.PRIVATE],
    )
    allowed, _ = evaluate_publish_policy(policy, Visibility.UNLISTED, OperatingMode.PRIVATE_TEST)
    assert allowed is False


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


def test_mock_adapter_deterministic() -> None:
    from clipos_api.publishing import PublishRequest

    adapter = MockPublishingAdapter()
    health = adapter.health_check("acct-1")
    assert health.ok is True
    assert set(health.capabilities) >= {"upload", "status"}
    request = PublishRequest(
        account_ref="acct",
        content_ref="storage://c.mp4",
        title="t",
        visibility=Visibility.PRIVATE,
        idempotency_key="key-1",
    )
    first = adapter.publish(request)
    second = adapter.publish(request)
    assert first.external_id == second.external_id
    assert first.status is PublishStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Policy store
# ---------------------------------------------------------------------------


def test_register_policy_idempotent(store: PublishingStore) -> None:
    first = store.register_policy(_policy())
    second = store.register_policy(_policy())
    assert first.policy_id == second.policy_id
    assert len(store.list_policies("org-1")) == 1


def test_register_policy_conflict_changed_fields(store: PublishingStore) -> None:
    store.register_policy(_policy())
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_policy(_policy(platform="tiktok"))


def test_register_policy_one_per_platform(store: PublishingStore) -> None:
    store.register_policy(_policy())
    with pytest.raises(ValueError, match="platform youtube"):
        store.register_policy(_policy(policy_id="policy-2"))


def test_get_list_org_scoped(store: PublishingStore) -> None:
    store.register_policy(_policy())
    assert store.get_policy("policy-1", "org-1") is not None
    assert store.get_policy("policy-1", "org-other") is None
    assert store.get_policy("missing", "org-1") is None
    assert store.list_policies("org-other") == []


# ---------------------------------------------------------------------------
# Submit publication flow
# ---------------------------------------------------------------------------


def test_submit_allowed_persists_succeeded(
    store: PublishingStore, engine: Engine, audit_log: Path
) -> None:
    store.register_policy(
        _policy(
            allowed_visibilities=[Visibility.PRIVATE, Visibility.UNLISTED, Visibility.PUBLIC],
            public_enabled=True,
        )
    )
    job = submit_publication(MockPublishingAdapter(), store, _submission())
    assert job.status is PublishStatus.SUCCEEDED
    assert job.external_id is not None
    assert job.decision_reason == "allowed"
    log = audit_log.read_text(encoding="utf-8")
    assert "publication_jobs.submitted" in log


def test_submit_denied_no_adapter_call(
    store: PublishingStore, engine: Engine, audit_log: Path
) -> None:
    # PUBLIC not in allowed_visibilities -> visibility_not_allowed
    store.register_policy(_policy(allowed_visibilities=[Visibility.PRIVATE, Visibility.UNLISTED]))
    job = submit_publication(MockPublishingAdapter(), store, _submission())
    assert job.status is PublishStatus.DENIED
    assert "visibility_not_allowed" in job.decision_reason
    log = audit_log.read_text(encoding="utf-8")
    assert "publication_jobs.denied" in log


def test_submit_idempotent_returns_stored(store: PublishingStore, engine: Engine) -> None:
    store.register_policy(_policy(public_enabled=True))
    first = submit_publication(MockPublishingAdapter(), store, _submission())
    second = submit_publication(MockPublishingAdapter(), store, _submission())
    assert first.job_id == second.job_id


def test_submit_conflict_changed_content(store: PublishingStore, engine: Engine) -> None:
    store.register_policy(_policy(public_enabled=True))
    submit_publication(MockPublishingAdapter(), store, _submission())
    with pytest.raises(ValueError, match="already recorded with different fields"):
        submit_publication(
            MockPublishingAdapter(), store, _submission(content_ref="storage://other.mp4")
        )


def test_submit_missing_policy_raises(store: PublishingStore, engine: Engine) -> None:
    with pytest.raises(ValueError, match="publish policy not found"):
        submit_publication(MockPublishingAdapter(), store, _submission())


def test_adapter_failure_persists_failed(store: PublishingStore, engine: Engine) -> None:
    store.register_policy(
        _policy(
            allowed_visibilities=[Visibility.PRIVATE, Visibility.UNLISTED, Visibility.PUBLIC],
            public_enabled=True,
        )
    )

    class BrokenAdapter:
        def health_check(self, acct):
            return AdapterHealth(ok=False)

        def publish(self, req):
            raise OSError("provider timeout")

        def get_status(self, ext_id):
            return PublishStatus.FAILED

    job = submit_publication(BrokenAdapter(), store, _submission())
    assert job.status is PublishStatus.FAILED
    assert "provider timeout" in (job.error_ref or "")


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/publish-policies" in paths
    assert "/publications" in paths
    assert "/publications/{job_id}" in paths


def test_router_meta() -> None:
    from clipos_api.publishing import router

    assert router.tags == ["publishing"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def test_route_full_publish_roundtrip(client) -> None:
    client.post(
        "/publish-policies",
        json={
            "policy_id": "policy-1",
            "org_id": "org-1",
            "platform": "youtube",
            "allowed_visibilities": ["PRIVATE", "UNLISTED", "PUBLIC"],
            "public_enabled": True,
            "kill_switch": False,
        },
    )
    response = client.post(
        "/publications",
        json={
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "acct-1",
            "content_ref": "storage://videos/clip-1.mp4",
            "title": "My clip",
            "visibility": "PUBLIC",
            "idempotency_key": "idem-001",
            "operating_mode": "GUARDED_PUBLIC",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert body["external_id"]

    fetched = client.get(f"/publications/{body['job_id']}", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "SUCCEEDED"

    listing = client.get("/publish-policies", params={"org_id": "org-1"})
    assert len(listing.json()) == 1


def test_route_denied_returns_200_with_denied_status(client) -> None:
    client.post(
        "/publish-policies",
        json={
            "policy_id": "policy-1",
            "org_id": "org-1",
            "platform": "youtube",
            "allowed_visibilities": ["PRIVATE", "UNLISTED"],
        },
    )
    response = client.post(
        "/publications",
        json={
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "acct-1",
            "content_ref": "storage://vid.mp4",
            "title": "Clip",
            "visibility": "PUBLIC",
            "idempotency_key": "idem-001",
            "operating_mode": "GUARDED_PUBLIC",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "DENIED"


def test_route_missing_policy_maps_to_404(client) -> None:
    response = client.post(
        "/publications",
        json={
            "org_id": "org-1",
            "platform": "no-platform",
            "account_ref": "acct-1",
            "content_ref": "storage://vid.mp4",
            "title": "Clip",
            "visibility": "PRIVATE",
            "idempotency_key": "idem-001",
            "operating_mode": "PRIVATE_TEST",
        },
    )
    assert response.status_code == 404


def test_route_policy_conflict_maps_to_409(client) -> None:
    payload = {
        "policy_id": "policy-1",
        "org_id": "org-1",
        "platform": "youtube",
    }
    assert client.post("/publish-policies", json=payload).status_code == 200
    payload["platform"] = "tiktok"
    assert client.post("/publish-policies", json=payload).status_code == 409


def test_route_job_missing_maps_to_404(client) -> None:
    response = client.get("/publications/missing", params={"org_id": "org-1"})
    assert response.status_code == 404
