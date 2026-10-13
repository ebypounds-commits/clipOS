"""Tests for brand/channel templates (CLIP-078)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.brand_templates import (
    BrandTemplateStore,
    RegisterBrandTemplateRequest,
    RegisterChannelTemplateRequest,
)
from clipos_api.main import app
from clipos_api.publishing import Visibility


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-078-templates-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> BrandTemplateStore:
    return BrandTemplateStore.from_engine(engine)


def _brand(**overrides: object) -> RegisterBrandTemplateRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "template_id": "brand-1",
        "title": "Main brand",
        "voice_ref": "voice://warm",
        "color_palette": ["#FF0000", "#abc"],
        "logo_ref": "asset://logo.png",
    }
    payload.update(overrides)
    return RegisterBrandTemplateRequest(**payload)  # type: ignore[arg-type]


def _channel(**overrides: object) -> RegisterChannelTemplateRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "template_id": "chan-1",
        "platform": "tiktok",
        "title": "TikTok default",
        "default_visibility": Visibility.PRIVATE,
        "caption_text": "New drop!",
        "hashtags": ["#drop", "#new"],
        "brand_template_id": "brand-1",
    }
    payload.update(overrides)
    return RegisterChannelTemplateRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


def test_palette_hex_validation() -> None:
    with pytest.raises(ValidationError, match="invalid hex color"):
        _brand(color_palette=["red"])
    with pytest.raises(ValidationError, match="invalid hex color"):
        _brand(color_palette=["#GGHHII"])


def test_hashtag_validation() -> None:
    with pytest.raises(ValidationError, match="invalid hashtag"):
        _channel(hashtags=["drop"])
    with pytest.raises(ValidationError, match="invalid hashtag"):
        _channel(hashtags=["#two words"])


# ---------------------------------------------------------------------------
# Brand store
# ---------------------------------------------------------------------------


def test_brand_register_idempotent_conflict_audit(
    store: BrandTemplateStore,
    audit_log: Path,
) -> None:
    first = store.register_brand(_brand())
    again = store.register_brand(_brand())
    assert first.template_id == again.template_id
    assert first.color_palette == ["#FF0000", "#abc"]
    assert "brand_templates.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_brand(_brand(title="Renamed"))
    assert len(store.list_brands("org-1")) == 1


def test_brand_get_org_scoped(store: BrandTemplateStore) -> None:
    store.register_brand(_brand())
    assert store.get_brand("brand-1", "org-1") is not None
    assert store.get_brand("brand-1", "org-other") is None
    assert store.get_brand("missing", "org-1") is None


# ---------------------------------------------------------------------------
# Channel store
# ---------------------------------------------------------------------------


def test_channel_register_idempotent_conflict_audit(
    store: BrandTemplateStore,
    audit_log: Path,
) -> None:
    store.register_brand(_brand())
    first = store.register_channel(_channel())
    again = store.register_channel(_channel())
    assert first.template_id == again.template_id
    assert first.default_visibility is Visibility.PRIVATE
    assert first.brand_template_id == "brand-1"
    assert "channel_templates.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_channel(_channel(title="Renamed"))


def test_channel_requires_brand_template(store: BrandTemplateStore) -> None:
    with pytest.raises(ValueError, match="brand template not found"):
        store.register_channel(_channel())


def test_channel_without_brand_ok(store: BrandTemplateStore) -> None:
    template = store.register_channel(_channel(brand_template_id=None))
    assert template.brand_template_id is None


def test_channel_platform_filter(store: BrandTemplateStore) -> None:
    store.register_brand(_brand())
    store.register_channel(_channel())
    store.register_channel(_channel(template_id="chan-2", platform="youtube"))
    assert len(store.list_channels("org-1")) == 2
    assert len(store.list_channels("org-1", "tiktok")) == 1
    assert len(store.list_channels("org-1", "youtube")) == 1
    assert len(store.list_channels("org-other")) == 0


def test_channel_get_org_scoped(store: BrandTemplateStore) -> None:
    store.register_brand(_brand())
    store.register_channel(_channel())
    assert store.get_channel("chan-1", "org-1") is not None
    assert store.get_channel("chan-1", "org-other") is None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/brand-templates" in paths
    assert "/brand-templates/{template_id}" in paths
    assert "/channel-templates" in paths
    assert "/channel-templates/{template_id}" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _brand_payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "template_id": "brand-1",
        "title": "Main brand",
        "voice_ref": "voice://warm",
        "color_palette": ["#FF0000", "#abc"],
        "logo_ref": "asset://logo.png",
    }


def test_full_route_roundtrip(client) -> None:
    created = client.post("/brand-templates", json=_brand_payload())
    assert created.status_code == 200, created.text
    fetched = client.get("/brand-templates/brand-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200

    conflict = client.post("/brand-templates", json={**_brand_payload(), "title": "X"})
    assert conflict.status_code == 409

    channel = client.post(
        "/channel-templates",
        json={
            "org_id": "org-1",
            "template_id": "chan-1",
            "platform": "tiktok",
            "title": "TikTok default",
            "default_visibility": "PRIVATE",
            "caption_text": "New drop!",
            "hashtags": ["#drop"],
            "brand_template_id": "brand-1",
        },
    )
    assert channel.status_code == 200, channel.text
    assert channel.json()["brand_template_id"] == "brand-1"

    listing = client.get("/channel-templates", params={"org_id": "org-1", "platform": "tiktok"})
    assert len(listing.json()) == 1

    fetched_channel = client.get("/channel-templates/chan-1", params={"org_id": "org-1"})
    assert fetched_channel.status_code == 200


def test_route_errors(client) -> None:
    missing_brand = client.get("/brand-templates/nope", params={"org_id": "org-1"})
    assert missing_brand.status_code == 404
    missing_channel = client.get("/channel-templates/nope", params={"org_id": "org-1"})
    assert missing_channel.status_code == 404

    orphan = client.post(
        "/channel-templates",
        json={
            "org_id": "org-1",
            "template_id": "chan-x",
            "platform": "tiktok",
            "title": "T",
            "brand_template_id": "missing",
        },
    )
    assert orphan.status_code == 404

    bad_hex = client.post("/brand-templates", json={**_brand_payload(), "color_palette": ["red"]})
    assert bad_hex.status_code == 422

    bad_tag = client.post(
        "/channel-templates",
        json={
            "org_id": "org-1",
            "template_id": "chan-y",
            "platform": "tiktok",
            "title": "T",
            "hashtags": ["nohash"],
        },
    )
    assert bad_tag.status_code == 422

    bad_slug = client.post("/brand-templates", json={**_brand_payload(), "template_id": "bad/id"})
    assert bad_slug.status_code == 422
