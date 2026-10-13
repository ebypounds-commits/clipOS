"""Tests for sponsorship campaign attribution (CLIP-072)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.sponsorship import (
    DeliverableStatus,
    DeliverableTransitionRequest,
    RecordAttributionRequest,
    RegisterCampaignRequest,
    RegisterDeliverableRequest,
    SponsorAttribution,
    SponsorCampaign,
    SponsorDeliverable,
    SponsorshipStore,
    summarize_sponsorship,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-072-sponsorship-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> SponsorshipStore:
    return SponsorshipStore.from_engine(engine)


def _campaign(**overrides: object) -> RegisterCampaignRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "campaign_id": "camp-1",
        "title": "Summer drop",
        "sponsor_ref": "sponsor://acme",
        "contracted_cents": 500_000,
        "currency": "USD",
        "starts_on": date(2026, 10, 1),
        "ends_on": date(2026, 10, 31),
    }
    payload.update(overrides)
    return RegisterCampaignRequest(**payload)  # type: ignore[arg-type]


def _deliverable(**overrides: object) -> RegisterDeliverableRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "deliverable_id": "del-1",
        "campaign_id": "camp-1",
        "deliverable_kind": "short-form-video",
        "content_ref": "content://video-1",
    }
    payload.update(overrides)
    return RegisterDeliverableRequest(**payload)  # type: ignore[arg-type]


def _attribution(**overrides: object) -> RecordAttributionRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "attribution_id": "attr-1",
        "campaign_id": "camp-1",
        "amount_cents": 250_000,
        "currency": "USD",
        "revenue_ref": "rev://payout-1",
    }
    payload.update(overrides)
    return RecordAttributionRequest(**payload)  # type: ignore[arg-type]


def _campaign_model(contracted_cents: int = 500_000, currency: str = "USD") -> SponsorCampaign:
    return SponsorCampaign(
        campaign_id="camp-1",
        org_id="org-1",
        title="Summer drop",
        sponsor_ref="sponsor://acme",
        contracted_cents=contracted_cents,
        currency=currency,
        starts_on=date(2026, 10, 1),
        ends_on=date(2026, 10, 31),
    )


def _deliverable_model(
    deliverable_id: str = "del-1",
    status: DeliverableStatus = DeliverableStatus.PENDING,
) -> SponsorDeliverable:
    return SponsorDeliverable(
        deliverable_id=deliverable_id,
        org_id="org-1",
        campaign_id="camp-1",
        deliverable_kind="short-form-video",
        content_ref=f"content://{deliverable_id}",
        status=status,
    )


def _attribution_model(
    attribution_id: str = "attr-1", amount_cents: int = 250_000, currency: str = "USD"
) -> SponsorAttribution:
    return SponsorAttribution(
        attribution_id=attribution_id,
        org_id="org-1",
        campaign_id="camp-1",
        amount_cents=amount_cents,
        currency=currency,
        revenue_ref="rev://payout",
    )


# ---------------------------------------------------------------------------
# Pure summary
# ---------------------------------------------------------------------------


def test_summary_empty() -> None:
    summary = summarize_sponsorship(_campaign_model(), [], [])
    assert summary.deliverable_count == 0
    assert summary.delivery_rate == 0.0
    assert summary.attributed_revenue_cents_by_currency == {}
    assert summary.variance_cents is None


def test_summary_delivery_and_revenue() -> None:
    deliverables = [
        _deliverable_model("del-1", DeliverableStatus.DELIVERED),
        _deliverable_model("del-2", DeliverableStatus.PENDING),
        _deliverable_model("del-3", DeliverableStatus.DELIVERED),
    ]
    attributions = [
        _attribution_model("attr-1", 300_000, "USD"),
        _attribution_model("attr-2", 50_000, "USD"),
        _attribution_model("attr-3", 200_000, "EUR"),
    ]
    summary = summarize_sponsorship(
        _campaign_model(contracted_cents=500_000), deliverables, attributions
    )
    assert summary.deliverable_count == 3
    assert summary.delivered_count == 2
    assert summary.delivery_rate == pytest.approx(2 / 3)
    assert summary.attributed_revenue_cents_by_currency == {"USD": 350_000, "EUR": 200_000}
    assert summary.variance_cents == 350_000 - 500_000


def test_summary_variance_none_when_currency_absent() -> None:
    summary = summarize_sponsorship(
        _campaign_model(currency="USD"),
        [],
        [_attribution_model(currency="EUR", amount_cents=100)],
    )
    assert summary.variance_cents is None


# ---------------------------------------------------------------------------
# Campaign store
# ---------------------------------------------------------------------------


def test_campaign_idempotent_conflict_audit(
    store: SponsorshipStore,
    audit_log: Path,
) -> None:
    first = store.register_campaign(_campaign())
    again = store.register_campaign(_campaign())
    assert first.campaign_id == again.campaign_id
    assert "sponsor_campaigns.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_campaign(_campaign(title="Changed"))
    assert len(store.list_campaigns("org-1")) == 1


def test_campaign_inverted_dates_rejected(store: SponsorshipStore) -> None:
    with pytest.raises(ValueError, match="ends_on must not precede"):
        store.register_campaign(_campaign(starts_on=date(2026, 10, 31), ends_on=date(2026, 10, 1)))


# ---------------------------------------------------------------------------
# Deliverable store
# ---------------------------------------------------------------------------


def test_deliverable_requires_campaign(
    store: SponsorshipStore,
    audit_log: Path,
) -> None:
    with pytest.raises(ValueError, match="sponsor campaign not found"):
        store.register_deliverable(_deliverable())
    store.register_campaign(_campaign())
    first = store.register_deliverable(_deliverable())
    again = store.register_deliverable(_deliverable())
    assert first.deliverable_id == again.deliverable_id
    assert first.status is DeliverableStatus.PENDING
    assert "sponsor_deliverables.registered" in audit_log.read_text(encoding="utf-8")


def test_deliverable_transition_one_shot(
    store: SponsorshipStore,
    audit_log: Path,
) -> None:
    store.register_campaign(_campaign())
    store.register_deliverable(_deliverable())
    delivered = store.transition_deliverable(
        "del-1",
        "org-1",
        DeliverableTransitionRequest(status=DeliverableStatus.DELIVERED, actor_ref="op"),
    )
    assert delivered.status is DeliverableStatus.DELIVERED
    assert "sponsor_deliverables.delivered" in audit_log.read_text(encoding="utf-8")
    # Same-state noop then illegal reverse.
    store.transition_deliverable(
        "del-1",
        "org-1",
        DeliverableTransitionRequest(status=DeliverableStatus.DELIVERED, actor_ref="op"),
    )
    with pytest.raises(ValueError, match="illegal deliverable transition"):
        store.transition_deliverable(
            "del-1",
            "org-1",
            DeliverableTransitionRequest(status=DeliverableStatus.PENDING, actor_ref="op"),
        )


def test_deliverable_unknown_404(store: SponsorshipStore) -> None:
    with pytest.raises(ValueError, match="deliverable not found"):
        store.transition_deliverable(
            "missing",
            "org-1",
            DeliverableTransitionRequest(status=DeliverableStatus.DELIVERED, actor_ref="op"),
        )


# ---------------------------------------------------------------------------
# Attribution store
# ---------------------------------------------------------------------------


def test_attribution_idempotent_requires_campaign(
    store: SponsorshipStore,
    audit_log: Path,
) -> None:
    with pytest.raises(ValueError, match="sponsor campaign not found"):
        store.record_attribution(_attribution())
    store.register_campaign(_campaign())
    first = store.record_attribution(_attribution())
    again = store.record_attribution(_attribution())
    assert first.attribution_id == again.attribution_id
    assert first.amount_cents == 250_000
    assert "sponsor_attributions.recorded" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.record_attribution(_attribution(amount_cents=1))


# ---------------------------------------------------------------------------
# Summary store
# ---------------------------------------------------------------------------


def test_summary_unknown_campaign_404(store: SponsorshipStore) -> None:
    with pytest.raises(ValueError, match="sponsor campaign not found"):
        store.summary("missing", "org-1")


def test_summary_end_to_end(store: SponsorshipStore) -> None:
    store.register_campaign(_campaign())
    store.register_deliverable(_deliverable())
    store.register_deliverable(_deliverable(deliverable_id="del-2"))
    store.transition_deliverable(
        "del-1",
        "org-1",
        DeliverableTransitionRequest(status=DeliverableStatus.DELIVERED, actor_ref="op"),
    )
    store.record_attribution(_attribution())
    summary = store.summary("camp-1", "org-1")
    assert summary.deliverable_count == 2
    assert summary.delivered_count == 1
    assert summary.delivery_rate == pytest.approx(0.5)
    assert summary.attributed_revenue_cents_by_currency == {"USD": 250_000}
    assert summary.variance_cents == 250_000 - 500_000


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/sponsor-campaigns" in paths
    assert "/sponsor-campaigns/{campaign_id}" in paths
    assert "/sponsor-campaigns/{campaign_id}/summary" in paths
    assert "/sponsor-campaigns/{campaign_id}/attributions" in paths
    assert "/sponsor-deliverables" in paths
    assert "/sponsor-deliverables/{deliverable_id}/transition" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _campaign_payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "campaign_id": "camp-1",
        "title": "Summer drop",
        "sponsor_ref": "sponsor://acme",
        "contracted_cents": 500_000,
        "currency": "USD",
        "starts_on": "2026-10-01",
        "ends_on": "2026-10-31",
    }


def test_full_route_roundtrip(client) -> None:
    created = client.post("/sponsor-campaigns", json=_campaign_payload())
    assert created.status_code == 200, created.text

    fetched = client.get("/sponsor-campaigns/camp-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200

    deliverable = client.post(
        "/sponsor-deliverables",
        json={
            "org_id": "org-1",
            "deliverable_id": "del-1",
            "campaign_id": "camp-1",
            "deliverable_kind": "short-form-video",
            "content_ref": "content://video-1",
        },
    )
    assert deliverable.status_code == 200, deliverable.text

    moved = client.post(
        "/sponsor-deliverables/del-1/transition",
        params={"org_id": "org-1"},
        json={"status": "DELIVERED", "actor_ref": "op"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == "DELIVERED"

    attribution = client.post(
        "/sponsor-campaigns/camp-1/attributions",
        json={
            "org_id": "org-1",
            "attribution_id": "attr-1",
            "campaign_id": "camp-1",
            "amount_cents": 250_000,
            "currency": "USD",
            "revenue_ref": "rev://payout-1",
        },
    )
    assert attribution.status_code == 200, attribution.text

    summary = client.post("/sponsor-campaigns/camp-1/summary", params={"org_id": "org-1"})
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["delivered_count"] == 1
    assert body["variance_cents"] == 250_000 - 500_000

    listing = client.get("/sponsor-campaigns", params={"org_id": "org-1"})
    assert len(listing.json()) == 1
    deliverables = client.get(
        "/sponsor-deliverables", params={"org_id": "org-1", "campaign_id": "camp-1"}
    )
    assert len(deliverables.json()) == 1


def test_route_errors(client) -> None:
    missing = client.get("/sponsor-campaigns/nope", params={"org_id": "org-1"})
    assert missing.status_code == 404

    assert client.post("/sponsor-campaigns", json=_campaign_payload()).status_code == 200
    conflict = client.post("/sponsor-campaigns", json={**_campaign_payload(), "title": "X"})
    assert conflict.status_code == 409

    # Path/body campaign_id mismatch -> 422.
    mismatch = client.post(
        "/sponsor-campaigns/camp-1/attributions",
        json={
            "org_id": "org-1",
            "attribution_id": "attr-x",
            "campaign_id": "other-camp",
            "amount_cents": 100,
            "currency": "USD",
            "revenue_ref": "rev://x",
        },
    )
    assert mismatch.status_code == 422

    bad_slug = client.post(
        "/sponsor-campaigns",
        json={**_campaign_payload(), "campaign_id": "bad/id"},
    )
    assert bad_slug.status_code == 422

    summary_missing = client.post("/sponsor-campaigns/missing/summary", params={"org_id": "org-1"})
    assert summary_missing.status_code == 404
