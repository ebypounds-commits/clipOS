"""Tests for platform accounts (CLIP-059)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.platform_accounts import (
    PlatformAccount,
    PlatformAccountStore,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-059-platform-accounts-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> PlatformAccountStore:
    return PlatformAccountStore.from_engine(engine)


def _account(**overrides: object) -> PlatformAccount:
    payload: dict[str, object] = {
        "account_id": "acct-1",
        "org_id": "org-1",
        "platform": "youtube",
        "account_ref": "yt-channel-abc",
        "display_name": "My YouTube channel",
        "channel_ref": "yt:UC-channel-id",
        "scopes": ["upload", "metadata"],
    }
    payload.update(overrides)
    return PlatformAccount(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_register_idempotent(store: PlatformAccountStore) -> None:
    first = store.register_account(_account())
    second = store.register_account(_account())
    assert first.account_id == second.account_id
    assert len(store.list_accounts("org-1")) == 1


def test_register_conflict_changed_fields(store: PlatformAccountStore) -> None:
    store.register_account(_account())
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_account(_account(display_name="Renamed channel"))


def test_register_one_per_platform_account_ref(store: PlatformAccountStore) -> None:
    store.register_account(_account())
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_account(_account(display_name="Different ref same key"))


def test_register_records_audit(store: PlatformAccountStore, audit_log: Path) -> None:
    store.register_account(_account())
    assert "platform_accounts.registered" in audit_log.read_text(encoding="utf-8")


def test_get_and_list_org_scoped(store: PlatformAccountStore) -> None:
    store.register_account(_account())
    assert store.get_account("acct-1", "org-1") is not None
    assert store.get_account("acct-1", "org-other") is None
    assert store.get_account("missing", "org-1") is None
    assert len(store.list_accounts("org-1")) == 1
    assert store.list_accounts("org-other") == []


def test_list_accounts_filter_by_platform(store: PlatformAccountStore) -> None:
    store.register_account(_account())
    store.register_account(
        _account(
            account_id="acct-2",
            platform="tiktok",
            account_ref="tt-123",
        )
    )
    assert len(store.list_accounts("org-1")) == 2
    assert len(store.list_accounts("org-1", "youtube")) == 1
    assert len(store.list_accounts("org-1", "tiktok")) == 1
    assert len(store.list_accounts("org-1", "meta")) == 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/platform-accounts" in paths
    assert "/platform-accounts/{account_id}" in paths


def test_router_meta() -> None:
    from clipos_api.platform_accounts import router

    assert router.tags == ["platform-accounts"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient
    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def test_register_and_fetch_roundtrip(client) -> None:
    created = client.post(
        "/platform-accounts",
        json={
            "account_id": "acct-1",
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt-abc",
            "display_name": "My channel",
            "scopes": ["upload"],
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["platform"] == "youtube"

    fetched = client.get("/platform-accounts/acct-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert fetched.json()["account_ref"] == "yt-abc"

    listing = client.get("/platform-accounts", params={"org_id": "org-1", "platform": "youtube"})
    assert len(listing.json()) == 1
    assert listing.json()[0]["display_name"] == "My channel"


def test_missing_account_maps_to_404(client) -> None:
    response = client.get("/platform-accounts/missing", params={"org_id": "org-1"})
    assert response.status_code == 404


def test_conflict_maps_to_409(client) -> None:
    payload = {
        "account_id": "acct-1",
        "org_id": "org-1",
        "platform": "youtube",
        "account_ref": "yt-abc",
        "display_name": "My channel",
    }
    assert client.post("/platform-accounts", json=payload).status_code == 200
    payload["display_name"] = "Changed"
    assert client.post("/platform-accounts", json=payload).status_code == 409


# ---------------------------------------------------------------------------
# YouTube adapter integration with publishing (PEP 563 probe)
# ---------------------------------------------------------------------------


def test_youtube_adapter_registered() -> None:
    """The YouTube adapter registered itself at import time."""
    from clipos_api.publishing import get_adapter

    adapter = get_adapter("youtube")
    health = adapter.health_check("yt-1")
    assert health.ok is True
    assert "upload" in health.capabilities
    # YouTube-specific capabilities present
    assert any(cap in ("resumable", "metadata") for cap in health.capabilities)


def test_youtube_adapter_publish_deterministic() -> None:
    from clipos_api.publishing import PublishRequest
    from clipos_api.publishing import Visibility

    from clipos_api.publishing import get_adapter

    adapter = get_adapter("youtube")
    request = PublishRequest(
        account_ref="yt-1",
        content_ref="storage://vid.mp4",
        title="Test",
        visibility=Visibility.UNLISTED,
        idempotency_key="key-yt-1",
    )
    first = adapter.publish(request)
    second = adapter.publish(request)
    assert first.external_id == second.external_id
    assert first.external_id.startswith("yt-mock-")
    assert first.status.value == "SUCCEEDED"
    assert adapter.get_status(first.external_id).value == "SUCCEEDED"


def test_unregistered_platform_falls_back_to_mock() -> None:
    from clipos_api.publishing import get_adapter

    adapter = get_adapter("not-a-real-platform")
    health = adapter.health_check("acct")
    assert "mock" in (health.detail or "")
