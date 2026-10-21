"""Tests for Meta publishing adapter (CLIP-061)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

import clipos_api.meta_adapter  # triggers adapter registration
import clipos_api.tiktok_adapter  # triggers adapter registration
import clipos_api.youtube_adapter  # noqa: F401  # triggers adapter registration
from clipos_api import audit
from clipos_api.publishing import (
    OperatingMode,
    PublishingStore,
    PublishPolicy,
    PublishRequest,
    PublishStatus,
    PublishSubmission,
    Visibility,
    get_adapter,
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
    path = Path("/tmp") / "clipos-061-meta-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> PublishingStore:
    return PublishingStore.from_engine(engine)


# ---------------------------------------------------------------------------
# Adapter registration
# ---------------------------------------------------------------------------


def test_meta_adapter_registered() -> None:
    """The Meta adapter registered itself at import time."""
    adapter = get_adapter("meta")
    health = adapter.health_check("meta-1")
    assert health.ok is True
    assert "upload" in health.capabilities
    assert "page_publish" in health.capabilities
    assert "privacy" in health.capabilities


def test_meta_adapter_health_detail() -> None:
    adapter = get_adapter("meta")
    health = adapter.health_check("acct")
    assert "mock" in (health.detail or "").lower()


# ---------------------------------------------------------------------------
# Adapter publish / get_status
# ---------------------------------------------------------------------------


def test_meta_adapter_publish_deterministic() -> None:
    adapter = get_adapter("meta")
    request = PublishRequest(
        account_ref="meta-1",
        content_ref="storage://vid.mp4",
        title="Meta Test",
        visibility=Visibility.PRIVATE,
        idempotency_key="key-meta-1",
    )
    first = adapter.publish(request)
    second = adapter.publish(request)
    assert first.external_id == second.external_id
    assert first.external_id.startswith("meta-mock-")
    assert first.status == PublishStatus.SUCCEEDED
    assert adapter.get_status(first.external_id) == PublishStatus.SUCCEEDED


def test_meta_adapter_publish_different_keys() -> None:
    adapter = get_adapter("meta")
    r1 = PublishRequest(
        account_ref="meta-1",
        content_ref="storage://a.mp4",
        title="A",
        visibility=Visibility.UNLISTED,
        idempotency_key="key-a",
    )
    r2 = PublishRequest(
        account_ref="meta-1",
        content_ref="storage://b.mp4",
        title="B",
        visibility=Visibility.UNLISTED,
        idempotency_key="key-b",
    )
    assert adapter.publish(r1).external_id != adapter.publish(r2).external_id


# ---------------------------------------------------------------------------
# Platform isolation — all three adapters are distinct
# ---------------------------------------------------------------------------


def test_meta_youtube_tiktok_are_distinct() -> None:
    meta = get_adapter("meta")
    yt = get_adapter("youtube")
    tt = get_adapter("tiktok")
    assert meta is not yt
    assert meta is not tt
    assert yt is not tt

    req = PublishRequest(
        account_ref="acct",
        content_ref="storage://x.mp4",
        title="X",
        visibility=Visibility.PRIVATE,
        idempotency_key="shared-key-061",
    )
    meta_id = meta.publish(req).external_id
    yt_id = yt.publish(req).external_id
    tt_id = tt.publish(req).external_id
    assert meta_id != yt_id
    assert meta_id != tt_id
    assert meta_id.startswith("meta-mock-")
    assert yt_id.startswith("yt-mock-")
    assert tt_id.startswith("tt-mock-")


# ---------------------------------------------------------------------------
# Publishing route selects Meta adapter
# ---------------------------------------------------------------------------


def test_publish_route_uses_meta_adapter(
    store: PublishingStore,
    audit_log: Path,
) -> None:
    """submit_publication with platform=meta uses the Meta adapter."""
    store.register_policy(
        PublishPolicy(
            policy_id="pol-meta",
            org_id="org-1",
            platform="meta",
            allowed_visibilities=[Visibility.PRIVATE, Visibility.UNLISTED],
        )
    )
    request = PublishSubmission(
        org_id="org-1",
        platform="meta",
        account_ref="meta-1",
        content_ref="storage://vid.mp4",
        title="Meta Upload",
        visibility=Visibility.PRIVATE,
        idempotency_key="idem-meta-1",
        operating_mode=OperatingMode.PRIVATE_TEST,
    )
    # rights_clear: the rights gate is exercised in
    # tests/test_publication_rights_enforcement.py; these tests are about
    # which adapter the platform resolves to.
    job = submit_publication(get_adapter("meta"), store, request, rights_clear=True)
    assert job.status == PublishStatus.SUCCEEDED
    assert job.external_id is not None
    assert job.external_id.startswith("meta-mock-")


# ---------------------------------------------------------------------------
# Fallback for unregistered platform
# ---------------------------------------------------------------------------


def test_unregistered_platform_falls_back_to_mock() -> None:
    adapter = get_adapter("not-a-real-platform")
    health = adapter.health_check("acct")
    assert "mock" in (health.detail or "").lower()


# ---------------------------------------------------------------------------
# Route integration (PEP 563 TestClient probe)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_meta_publish_roundtrip_via_route(client, clear_rights) -> None:
    """Full route roundtrip: register policy, submit publication via Meta."""
    clear_rights("storage://vid.mp4", org_id="org-1")
    pol = client.post(
        "/publish-policies",
        json={
            "policy_id": "pol-meta-route",
            "org_id": "org-1",
            "platform": "meta",
            "allowed_visibilities": ["PRIVATE", "UNLISTED"],
        },
    )
    assert pol.status_code == 200, pol.text

    pub = client.post(
        "/publications",
        json={
            "org_id": "org-1",
            "platform": "meta",
            "account_ref": "meta-1",
            "content_ref": "storage://vid.mp4",
            "title": "Route Meta Test",
            "visibility": "PRIVATE",
            "idempotency_key": "route-meta-1",
            "operating_mode": "PRIVATE_TEST",
        },
    )
    assert pub.status_code == 200, pub.text
    body = pub.json()
    assert body["status"] == "SUCCEEDED"
    assert body["external_id"].startswith("meta-mock-")

    fetched = client.get(f"/publications/{body['job_id']}", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert fetched.json()["external_id"] == body["external_id"]


def test_meta_denied_returns_200_with_denied_status(client, clear_rights) -> None:
    """Meta submission denied by policy returns 200 with DENIED."""
    clear_rights("storage://vid.mp4", org_id="org-2")
    client.post(
        "/publish-policies",
        json={
            "policy_id": "pol-meta-deny",
            "org_id": "org-2",
            "platform": "meta",
            "allowed_visibilities": ["PRIVATE"],
        },
    )
    resp = client.post(
        "/publications",
        json={
            "org_id": "org-2",
            "platform": "meta",
            "account_ref": "meta-2",
            "content_ref": "storage://vid.mp4",
            "title": "Denied",
            "visibility": "PUBLIC",
            "idempotency_key": "route-meta-deny",
            "operating_mode": "PRIVATE_TEST",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "DENIED"
    assert resp.json()["external_id"] is None
    # The rights precondition is cleared, so the refusal is the visibility
    # rule under test — not an incidental rights denial.
    assert "visibility" in resp.json()["decision_reason"]
