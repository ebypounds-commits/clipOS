"""Tests for affiliate link/click/conversion attribution (CLIP-071)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.affiliate_attribution import (
    AffiliateClick,
    AffiliateConversion,
    AffiliateStore,
    RecordClickRequest,
    RecordConversionRequest,
    RegisterLinkRequest,
    summarize_attribution,
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
    path = Path("/tmp") / "clipos-071-affiliate-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> AffiliateStore:
    return AffiliateStore.from_engine(engine)


def _link(**overrides: object) -> RegisterLinkRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "link_id": "link-1",
        "campaign_ref": "campaign://summer",
        "destination_ref": "dest://offer-1",
        "content_ref": "content://video-1",
    }
    payload.update(overrides)
    return RegisterLinkRequest(**payload)  # type: ignore[arg-type]


def _click(**overrides: object) -> RecordClickRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "click_id": "click-1",
        "link_id": "link-1",
        "visitor_ref": "visitor-abc",
        "referrer_ref": "ref://social",
    }
    payload.update(overrides)
    return RecordClickRequest(**payload)  # type: ignore[arg-type]


def _conversion(**overrides: object) -> RecordConversionRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "conversion_id": "conv-1",
        "link_id": "link-1",
        "click_ref": "click-1",
        "amount_cents": 1999,
        "currency": "USD",
        "order_ref": "order-777",
    }
    payload.update(overrides)
    return RecordConversionRequest(**payload)  # type: ignore[arg-type]


def _click_model(click_id: str = "click-1", link_id: str = "link-1") -> AffiliateClick:
    return AffiliateClick(
        click_id=click_id,
        org_id="org-1",
        link_id=link_id,
        visitor_ref="visitor",
        referrer_ref=None,
    )


def _conversion_model(
    conversion_id: str = "conv-1",
    link_id: str = "link-1",
    amount_cents: int = 1000,
    currency: str = "USD",
) -> AffiliateConversion:
    return AffiliateConversion(
        conversion_id=conversion_id,
        org_id="org-1",
        link_id=link_id,
        click_ref=None,
        amount_cents=amount_cents,
        currency=currency,
        order_ref="order",
    )


# ---------------------------------------------------------------------------
# Pure attribution
# ---------------------------------------------------------------------------


def test_attribution_empty() -> None:
    summary = summarize_attribution("link-1", "org-1", [], [])
    assert summary.click_count == 0
    assert summary.conversion_count == 0
    assert summary.conversion_rate == 0.0
    assert summary.revenue_cents_by_currency == {}


def test_attribution_rate_and_per_currency_revenue() -> None:
    clicks = [_click_model(f"c{i}") for i in range(4)]
    conversions = [
        _conversion_model("conv-1", amount_cents=1000, currency="USD"),
        _conversion_model("conv-2", amount_cents=500, currency="USD"),
        _conversion_model("conv-3", amount_cents=250, currency="EUR"),
    ]
    summary = summarize_attribution("link-1", "org-1", clicks, conversions)
    assert summary.click_count == 4
    assert summary.conversion_count == 3
    assert summary.conversion_rate == pytest.approx(0.75)
    assert summary.revenue_cents_by_currency == {"USD": 1500, "EUR": 250}


# ---------------------------------------------------------------------------
# Links store
# ---------------------------------------------------------------------------


def test_link_register_idempotent_conflict_audit(
    store: AffiliateStore,
    audit_log: Path,
) -> None:
    first = store.register_link(_link())
    second = store.register_link(_link())
    assert first.link_id == second.link_id
    assert "affiliate_links.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_link(_link(destination_ref="dest://other"))
    assert len(store.list_links("org-1")) == 1


def test_link_get_list_org_scoped(store: AffiliateStore) -> None:
    store.register_link(_link())
    assert store.get_link("link-1", "org-1") is not None
    assert store.get_link("link-1", "org-other") is None
    assert len(store.list_links("org-other")) == 0


# ---------------------------------------------------------------------------
# Clicks store
# ---------------------------------------------------------------------------


def test_click_idempotent_requires_link(
    store: AffiliateStore,
    audit_log: Path,
) -> None:
    store.register_link(_link())
    first = store.record_click(_click())
    again = store.record_click(_click())
    assert first.click_id == again.click_id
    assert "affiliate_clicks.recorded" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.record_click(_click(visitor_ref="other-visitor"))
    with pytest.raises(ValueError, match="affiliate link not found"):
        store.record_click(_click(link_id="missing"))


def test_clicks_list_filter(store: AffiliateStore) -> None:
    store.register_link(_link())
    store.register_link(_link(link_id="link-2"))
    store.record_click(_click())
    store.record_click(_click(click_id="click-2", link_id="link-2"))
    assert len(store.list_clicks("org-1")) == 2
    assert len(store.list_clicks("org-1", "link-1")) == 1
    assert len(store.list_clicks("org-1", "link-2")) == 1


# ---------------------------------------------------------------------------
# Conversions store
# ---------------------------------------------------------------------------


def test_conversion_with_click_attribution(
    store: AffiliateStore,
    audit_log: Path,
) -> None:
    store.register_link(_link())
    store.record_click(_click())
    first = store.record_conversion(_conversion())
    again = store.record_conversion(_conversion())
    assert first.conversion_id == again.conversion_id
    assert first.amount_cents == 1999
    assert first.currency == "USD"
    assert "affiliate_conversions.recorded" in audit_log.read_text(encoding="utf-8")


def test_conversion_invalid_attribution(store: AffiliateStore) -> None:
    store.register_link(_link())
    store.register_link(_link(link_id="link-2"))
    store.record_click(_click())
    with pytest.raises(ValueError, match="affiliate click not found"):
        store.record_conversion(_conversion(click_ref="missing"))
    with pytest.raises(ValueError, match="affiliate click not found"):
        store.record_conversion(_conversion(click_ref="click-1", link_id="link-2"))


def test_conversion_conflict_and_missing_link(store: AffiliateStore) -> None:
    store.register_link(_link())
    store.record_click(_click())
    store.record_conversion(_conversion())
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.record_conversion(_conversion(amount_cents=500))
    with pytest.raises(ValueError, match="affiliate link not found"):
        store.record_conversion(_conversion(link_id="missing", conversion_id="conv-2"))


# ---------------------------------------------------------------------------
# Attribution store
# ---------------------------------------------------------------------------


def test_attribution_unknown_link_404(store: AffiliateStore) -> None:
    with pytest.raises(ValueError, match="affiliate link not found"):
        store.attribution("missing", "org-1")


def test_attribution_end_to_end(store: AffiliateStore) -> None:
    store.register_link(_link())
    for i in range(3):
        store.record_click(_click(click_id=f"click-{i}"))
    store.record_conversion(_conversion())
    store.record_conversion(
        _conversion(
            conversion_id="conv-2",
            click_ref="click-1",
            amount_cents=500,
        )
    )
    summary = store.attribution("link-1", "org-1")
    assert summary.click_count == 3
    assert summary.conversion_count == 2
    assert summary.conversion_rate == pytest.approx(2 / 3)
    assert summary.revenue_cents_by_currency == {"USD": 2499}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/affiliate-links" in paths
    assert "/affiliate-links/{link_id}" in paths
    assert "/affiliate-links/{link_id}/attribution" in paths
    assert "/affiliate-clicks" in paths
    assert "/affiliate-conversions" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_full_route_roundtrip(client) -> None:
    link = client.post(
        "/affiliate-links",
        json={
            "org_id": "org-1",
            "link_id": "link-1",
            "campaign_ref": "campaign://summer",
            "destination_ref": "dest://offer-1",
        },
    )
    assert link.status_code == 200, link.text

    click = client.post(
        "/affiliate-clicks",
        json={
            "org_id": "org-1",
            "click_id": "click-1",
            "link_id": "link-1",
            "visitor_ref": "visitor-abc",
        },
    )
    assert click.status_code == 200, click.text

    conv = client.post(
        "/affiliate-conversions",
        json={
            "org_id": "org-1",
            "conversion_id": "conv-1",
            "link_id": "link-1",
            "click_ref": "click-1",
            "amount_cents": 1999,
            "currency": "USD",
            "order_ref": "order-777",
        },
    )
    assert conv.status_code == 200, conv.text

    attribution = client.get("/affiliate-links/link-1/attribution", params={"org_id": "org-1"})
    assert attribution.status_code == 200, attribution.text
    body = attribution.json()
    assert body["click_count"] == 1
    assert body["conversion_count"] == 1
    assert body["revenue_cents_by_currency"] == {"USD": 1999}

    clicks = client.get("/affiliate-clicks", params={"org_id": "org-1", "link_id": "link-1"})
    assert len(clicks.json()) == 1
    conversions = client.get(
        "/affiliate-conversions", params={"org_id": "org-1", "link_id": "link-1"}
    )
    assert len(conversions.json()) == 1


def test_route_404s_and_conflicts(client) -> None:
    missing_link = client.get("/affiliate-links/nope", params={"org_id": "org-1"})
    assert missing_link.status_code == 404

    bad_click = client.post(
        "/affiliate-clicks",
        json={
            "org_id": "org-1",
            "click_id": "click-x",
            "link_id": "missing",
            "visitor_ref": "v",
        },
    )
    assert bad_click.status_code == 404

    attr_missing = client.get("/affiliate-links/missing/attribution", params={"org_id": "org-1"})
    assert attr_missing.status_code == 404

    client.post(
        "/affiliate-links",
        json={
            "org_id": "org-1",
            "link_id": "link-1",
            "campaign_ref": "c",
            "destination_ref": "d",
        },
    )
    conflict = client.post(
        "/affiliate-links",
        json={
            "org_id": "org-1",
            "link_id": "link-1",
            "campaign_ref": "c2",
            "destination_ref": "d",
        },
    )
    assert conflict.status_code == 409

    bad_slug = client.post(
        "/affiliate-links",
        json={
            "org_id": "org-1",
            "link_id": "bad/id",
            "campaign_ref": "c",
            "destination_ref": "d",
        },
    )
    assert bad_slug.status_code == 422
