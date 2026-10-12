"""Tests for content briefs and the content-plan engine (CLIP-047)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.content_planning import (
    ContentBrief,
    ContentPlanningStore,
    derive_plan_payload,
)
from clipos_api.main import app
from clipos_api.research import (
    Claim,
    ClaimVeracity,
    ResearchPacket,
    ResearchStore,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-047-content-planning-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ContentPlanningStore:
    return ContentPlanningStore.from_engine(engine)


def _seed_research(engine: Engine, verified: int = 1, unverified: int = 0) -> None:
    research = ResearchStore.from_engine(engine)
    research.register_packet(
        ResearchPacket(
            research_packet_id="packet-1",
            org_id="org-1",
            title="Fixture packet",
        )
    )
    for index in range(verified):
        research.register_claim(
            Claim(
                claim_id=f"claim-v{index}",
                org_id="org-1",
                research_packet_id="packet-1",
                statement=f"Verified statement {index}",
            )
        )
        research.set_veracity(
            f"claim-v{index}",
            "org-1",
            ClaimVeracity.VERIFIED,
            actor_ref="reviewer-1",
        )
    for index in range(unverified):
        research.register_claim(
            Claim(
                claim_id=f"claim-u{index}",
                org_id="org-1",
                research_packet_id="packet-1",
                statement=f"Unverified statement {index}",
            )
        )


def _brief(brief_id: str = "brief-1", **overrides: object) -> ContentBrief:
    payload: dict[str, object] = {
        "brief_id": brief_id,
        "org_id": "org-1",
        "research_packet_id": "packet-1",
        "title": "Fixture brief",
        "objective": "Produce a rights-safe vertical cut",
        "platforms": ["youtube", "tiktok"],
    }
    payload.update(overrides)
    return ContentBrief(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure derivation
# ---------------------------------------------------------------------------


def test_derive_payload_sorts_claims_and_counts() -> None:
    claims = [
        type("Row", (), {"claim_id": "b"})(),
        type("Row", (), {"claim_id": "a"})(),
    ]
    payload = derive_plan_payload(_brief(), claims)  # type: ignore[arg-type]
    assert payload["claim_ids"] == ["a", "b"]
    assert payload["claim_count"] == 2
    assert payload["platforms"] == ["youtube", "tiktok"]
    assert payload["objective"] == "Produce a rights-safe vertical cut"


def test_brief_platform_entry_validation() -> None:
    with pytest.raises(Exception, match="platform entries"):
        _brief(platforms=[""])
    with pytest.raises(Exception, match="platform entries"):
        _brief(platforms=["x" * 65])


# ---------------------------------------------------------------------------
# Brief registration
# ---------------------------------------------------------------------------


def test_register_brief_requires_existing_packet(store: ContentPlanningStore) -> None:
    with pytest.raises(ValueError, match="research packet not found"):
        store.register_brief(_brief())


def test_register_brief_idempotent(store: ContentPlanningStore, engine: Engine) -> None:
    _seed_research(engine)
    first = store.register_brief(_brief())
    second = store.register_brief(_brief())
    assert first.brief_id == second.brief_id
    assert first.platforms == ["youtube", "tiktok"]
    assert len(store.list_briefs("org-1")) == 1


def test_register_brief_conflict_on_different_fields(
    store: ContentPlanningStore, engine: Engine
) -> None:
    _seed_research(engine)
    store.register_brief(_brief())
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_brief(_brief(title="Different title"))


def test_get_brief_org_scoped(store: ContentPlanningStore, engine: Engine) -> None:
    _seed_research(engine)
    store.register_brief(_brief())
    assert store.get_brief("brief-1", "org-1") is not None
    assert store.get_brief("brief-1", "org-other") is None
    assert store.get_brief("missing", "org-1") is None


def test_register_brief_records_audit(
    store: ContentPlanningStore, engine: Engine, audit_log: Path
) -> None:
    _seed_research(engine)
    store.register_brief(_brief())
    assert "content_briefs.registered" in audit_log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Plan engine
# ---------------------------------------------------------------------------


def test_generate_plan_requires_brief(store: ContentPlanningStore) -> None:
    with pytest.raises(ValueError, match="content brief not found"):
        store.generate_plan("missing", "org-1")


def test_generate_plan_fails_closed_without_verified_claims(
    store: ContentPlanningStore, engine: Engine
) -> None:
    _seed_research(engine, verified=0, unverified=2)
    store.register_brief(_brief())
    with pytest.raises(ValueError, match="no verified claims available"):
        store.generate_plan("brief-1", "org-1")


def test_generate_plan_excludes_unverified_claims(
    store: ContentPlanningStore, engine: Engine
) -> None:
    _seed_research(engine, verified=1, unverified=2)
    store.register_brief(_brief())
    plan = store.generate_plan("brief-1", "org-1")
    assert plan.version == 1
    assert plan.payload["claim_ids"] == ["claim-v0"]
    assert plan.payload["claim_count"] == 1
    assert plan.payload["brief_id"] == "brief-1"


def test_generate_plan_identical_rerun_is_noop(
    store: ContentPlanningStore, engine: Engine, audit_log: Path
) -> None:
    _seed_research(engine, verified=1)
    store.register_brief(_brief())
    first = store.generate_plan("brief-1", "org-1")
    second = store.generate_plan("brief-1", "org-1")
    assert first.plan_id == second.plan_id
    assert first.version == second.version == 1
    log = audit_log.read_text(encoding="utf-8")
    assert log.count("content_plans.generated") == 1


def test_generate_plan_versions_when_claims_change(
    store: ContentPlanningStore, engine: Engine
) -> None:
    _seed_research(engine, verified=1, unverified=1)
    research = ResearchStore.from_engine(engine)
    store.register_brief(_brief())
    first = store.generate_plan("brief-1", "org-1")
    assert first.version == 1
    assert first.payload["claim_count"] == 1

    # verify the second claim -> derived payload changes -> version 2
    research.set_veracity("claim-u0", "org-1", ClaimVeracity.VERIFIED, actor_ref="reviewer-1")
    second = store.generate_plan("brief-1", "org-1")
    assert second.version == 2
    assert second.payload["claim_count"] == 2
    latest = store.get_latest_plan("brief-1", "org-1")
    assert latest is not None
    assert latest.version == 2


def test_get_latest_plan_missing_returns_none(store: ContentPlanningStore) -> None:
    assert store.get_latest_plan("missing", "org-1") is None


def test_plan_audit_recorded(store: ContentPlanningStore, engine: Engine, audit_log: Path) -> None:
    _seed_research(engine)
    store.register_brief(_brief())
    store.generate_plan("brief-1", "org-1")
    assert "content_plans.generated" in audit_log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/content-briefs" in paths
    assert "/content-briefs/{brief_id}" in paths
    assert "/content-briefs/{brief_id}/plan" in paths


def test_router_meta() -> None:
    from clipos_api.content_planning import router

    assert router.tags == ["content-planning"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def _seed_research_http(client) -> None:
    assert (
        client.post(
            "/research-packets",
            json={"research_packet_id": "packet-1", "org_id": "org-1", "title": "Fixture"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/research-packets/packet-1/claims",
            json={
                "claim_id": "claim-1",
                "org_id": "org-1",
                "research_packet_id": "packet-1",
                "statement": "A verified statement",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/claims/claim-1/veracity",
            json={"org_id": "org-1", "veracity": "VERIFIED", "actor_ref": "reviewer-1"},
        ).status_code
        == 200
    )


def test_route_brief_to_plan_roundtrip(client) -> None:
    _seed_research_http(client)
    brief_response = client.post(
        "/content-briefs",
        json={
            "brief_id": "brief-1",
            "org_id": "org-1",
            "research_packet_id": "packet-1",
            "title": "Fixture brief",
            "objective": "Produce a rights-safe vertical cut",
            "platforms": ["youtube", "tiktok"],
        },
    )
    assert brief_response.status_code == 200
    assert brief_response.json()["brief_id"] == "brief-1"

    plan_response = client.post("/content-briefs/brief-1/plan", json={"org_id": "org-1"})
    assert plan_response.status_code == 200
    body = plan_response.json()
    assert body["version"] == 1
    assert body["payload"]["claim_ids"] == ["claim-1"]

    latest = client.get("/content-briefs/brief-1/plan", params={"org_id": "org-1"})
    assert latest.status_code == 200
    assert latest.json()["version"] == 1

    # idempotent re-derive over HTTP
    again = client.post("/content-briefs/brief-1/plan", json={"org_id": "org-1"})
    assert again.status_code == 200
    assert again.json()["plan_id"] == body["plan_id"]


def test_route_plan_without_verified_claims_maps_to_422(client) -> None:
    _seed_research_http(client)
    # flip back to UNVERIFIED? claim was verified above; register brief with a
    # fresh packet that has no verified claims instead.
    client.post(
        "/research-packets",
        json={"research_packet_id": "packet-2", "org_id": "org-1", "title": "Empty"},
    )
    client.post(
        "/research-packets/packet-2/claims",
        json={
            "claim_id": "claim-unchecked",
            "org_id": "org-1",
            "research_packet_id": "packet-2",
            "statement": "Still unverified",
        },
    )
    client.post(
        "/content-briefs",
        json={
            "brief_id": "brief-2",
            "org_id": "org-1",
            "research_packet_id": "packet-2",
            "title": "Blocked brief",
            "objective": "Blocked until claims verify",
        },
    )
    response = client.post("/content-briefs/brief-2/plan", json={"org_id": "org-1"})
    assert response.status_code == 422
    assert "no verified claims" in response.json()["detail"]


def test_route_brief_missing_maps_to_404(client) -> None:
    response = client.get("/content-briefs/missing", params={"org_id": "org-1"})
    assert response.status_code == 404
    plan = client.post("/content-briefs/missing/plan", json={"org_id": "org-1"})
    assert plan.status_code == 404


def test_route_brief_requires_existing_packet(client) -> None:
    response = client.post(
        "/content-briefs",
        json={
            "brief_id": "brief-1",
            "org_id": "org-1",
            "research_packet_id": "missing-packet",
            "title": "Fixture",
            "objective": "Fixture objective",
        },
    )
    assert response.status_code == 404


def test_route_list_briefs(client) -> None:
    _seed_research_http(client)
    client.post(
        "/content-briefs",
        json={
            "brief_id": "brief-1",
            "org_id": "org-1",
            "research_packet_id": "packet-1",
            "title": "Fixture brief",
            "objective": "Produce a rights-safe vertical cut",
        },
    )
    listing = client.get("/content-briefs", params={"org_id": "org-1"})
    assert listing.status_code == 200
    assert len(listing.json()) == 1
    assert listing.json()[0]["brief_id"] == "brief-1"
