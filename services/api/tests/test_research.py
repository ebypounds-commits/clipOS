"""Tests for research packets, claims, and claim sources (CLIP-046)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.research import (
    Claim,
    ClaimSourceRequest,
    ClaimVeracity,
    ResearchPacket,
    ResearchStore,
    SourceSupport,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-046-research-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ResearchStore:
    return ResearchStore.from_engine(engine)


def _packet(
    packet_id: str = "packet-1", org_id: str = "org-1", **overrides: object
) -> ResearchPacket:
    payload: dict[str, object] = {
        "research_packet_id": packet_id,
        "org_id": org_id,
        "title": "Fixture packet",
        "summary": "A short summary",
    }
    payload.update(overrides)
    return ResearchPacket(**payload)  # type: ignore[arg-type]


def _claim(claim_id: str = "claim-1", packet_id: str = "packet-1", **overrides: object) -> Claim:
    payload: dict[str, object] = {
        "claim_id": claim_id,
        "org_id": "org-1",
        "research_packet_id": packet_id,
        "statement": "The fixture claim statement",
    }
    payload.update(overrides)
    return Claim(**payload)  # type: ignore[arg-type]


def _seed_packet_and_claim(store: ResearchStore) -> None:
    store.register_packet(_packet())
    store.register_claim(_claim())


# ---------------------------------------------------------------------------
# Research packets
# ---------------------------------------------------------------------------


def test_register_packet_idempotent(store: ResearchStore) -> None:
    first = store.register_packet(_packet())
    second = store.register_packet(_packet())
    assert first.research_packet_id == second.research_packet_id
    assert len(store.list_packets("org-1")) == 1


def test_register_packet_conflict_on_different_fields(store: ResearchStore) -> None:
    store.register_packet(_packet())
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_packet(_packet(title="Different title"))


def test_register_packet_org_scoped(store: ResearchStore) -> None:
    store.register_packet(_packet())
    assert store.get_packet("packet-1", "org-1") is not None
    assert store.get_packet("packet-1", "org-other") is None
    assert store.list_packets("org-other") == []


def test_register_packet_records_audit(store: ResearchStore, audit_log: Path) -> None:
    store.register_packet(_packet())
    assert "research_packets.registered" in audit_log.read_text(encoding="utf-8")


def test_get_packet_missing_returns_none(store: ResearchStore) -> None:
    assert store.get_packet("missing", "org-1") is None


def test_register_packet_with_topic_link(store: ResearchStore) -> None:
    stored = store.register_packet(_packet(topic_id="topic-9"))
    assert stored.topic_id == "topic-9"


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


def test_register_claim_requires_existing_packet(store: ResearchStore) -> None:
    with pytest.raises(ValueError, match="research packet not found"):
        store.register_claim(_claim(packet_id="missing-packet"))


def test_register_claim_idempotent(store: ResearchStore) -> None:
    _seed_packet_and_claim(store)
    first = store.register_claim(_claim())
    second = store.register_claim(_claim())
    assert first.claim_id == second.claim_id
    assert first.veracity is ClaimVeracity.UNVERIFIED
    assert len(store.list_claims("packet-1", "org-1")) == 1


def test_register_claim_conflict_on_different_statement(store: ResearchStore) -> None:
    _seed_packet_and_claim(store)
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_claim(_claim(statement="A different statement entirely"))


def test_register_claim_scoped_to_packet(store: ResearchStore) -> None:
    store.register_packet(_packet())
    store.register_packet(_packet(packet_id="packet-2", title="Other"))
    store.register_claim(_claim(claim_id="claim-1", packet_id="packet-1"))
    store.register_claim(_claim(claim_id="claim-2", packet_id="packet-2"))
    packet_one_claims = store.list_claims("packet-1", "org-1")
    assert [claim.claim_id for claim in packet_one_claims] == ["claim-1"]
    assert store.list_claims("packet-1", "org-other") == []


def test_claim_audit_recorded(store: ResearchStore, audit_log: Path) -> None:
    _seed_packet_and_claim(store)
    assert "claims.registered" in audit_log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Veracity
# ---------------------------------------------------------------------------


def test_set_veracity_transition_audited(store: ResearchStore, audit_log: Path) -> None:
    _seed_packet_and_claim(store)
    updated = store.set_veracity("claim-1", "org-1", ClaimVeracity.VERIFIED, actor_ref="reviewer-1")
    assert updated.veracity is ClaimVeracity.VERIFIED
    assert updated.claim_id == "claim-1"
    log = audit_log.read_text(encoding="utf-8")
    assert "claims.veracity_changed" in log
    assert "reviewer-1" in log


def test_set_veracity_same_value_is_noop(store: ResearchStore, audit_log: Path) -> None:
    _seed_packet_and_claim(store)
    store.set_veracity("claim-1", "org-1", ClaimVeracity.UNVERIFIED, actor_ref="reviewer-1")
    log = audit_log.read_text(encoding="utf-8")
    assert "claims.veracity_changed" not in log


def test_set_veracity_allows_reopen_to_unverified(store: ResearchStore) -> None:
    _seed_packet_and_claim(store)
    store.set_veracity("claim-1", "org-1", ClaimVeracity.DISPUTED, actor_ref="reviewer-1")
    reopened = store.set_veracity(
        "claim-1", "org-1", ClaimVeracity.UNVERIFIED, actor_ref="reviewer-1"
    )
    assert reopened.veracity is ClaimVeracity.UNVERIFIED


def test_set_veracity_missing_claim_raises(store: ResearchStore) -> None:
    with pytest.raises(ValueError, match="claim not found"):
        store.set_veracity("missing", "org-1", ClaimVeracity.VERIFIED, actor_ref="r")


# ---------------------------------------------------------------------------
# Claim sources
# ---------------------------------------------------------------------------


def test_link_source_requires_existing_claim(store: ResearchStore) -> None:
    with pytest.raises(ValueError, match="claim not found"):
        store.link_source(
            ClaimSourceRequest(org_id="org-1", source_ref="https://example.test/a"),
            "missing",
        )


def test_link_source_idempotent(store: ResearchStore) -> None:
    _seed_packet_and_claim(store)
    request = ClaimSourceRequest(org_id="org-1", source_ref="https://example.test/a")
    first = store.link_source(request, "claim-1")
    second = store.link_source(request, "claim-1")
    assert first.claim_source_id == second.claim_source_id
    assert len(store.list_sources("claim-1", "org-1")) == 1


def test_link_source_conflict_on_different_support(store: ResearchStore) -> None:
    _seed_packet_and_claim(store)
    store.link_source(
        ClaimSourceRequest(
            org_id="org-1", source_ref="https://example.test/a", support=SourceSupport.SUPPORTS
        ),
        "claim-1",
    )
    with pytest.raises(ValueError, match="already linked with different fields"):
        store.link_source(
            ClaimSourceRequest(
                org_id="org-1",
                source_ref="https://example.test/a",
                support=SourceSupport.CONTRADICTS,
            ),
            "claim-1",
        )


def test_link_source_records_audit(store: ResearchStore, audit_log: Path) -> None:
    _seed_packet_and_claim(store)
    store.link_source(
        ClaimSourceRequest(org_id="org-1", source_ref="https://example.test/a"),
        "claim-1",
    )
    assert "claim_sources.linked" in audit_log.read_text(encoding="utf-8")


def test_list_sources_org_scoped(store: ResearchStore) -> None:
    _seed_packet_and_claim(store)
    store.link_source(
        ClaimSourceRequest(org_id="org-1", source_ref="https://example.test/a"),
        "claim-1",
    )
    assert len(store.list_sources("claim-1", "org-1")) == 1
    assert store.list_sources("claim-1", "org-other") == []


def test_multiple_sources_per_claim(store: ResearchStore) -> None:
    _seed_packet_and_claim(store)
    store.link_source(
        ClaimSourceRequest(org_id="org-1", source_ref="https://example.test/a"),
        "claim-1",
    )
    store.link_source(
        ClaimSourceRequest(
            org_id="org-1",
            source_ref="https://example.test/b",
            support=SourceSupport.CONTRADICTS,
            quote_ref="quote:9",
        ),
        "claim-1",
    )
    sources = store.list_sources("claim-1", "org-1")
    assert len(sources) == 2
    supports = {source.support for source in sources}
    assert supports == {SourceSupport.SUPPORTS, SourceSupport.CONTRADICTS}


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/research-packets" in paths
    assert "/research-packets/{research_packet_id}" in paths
    assert "/research-packets/{research_packet_id}/claims" in paths
    assert "/claims/{claim_id}/sources" in paths
    assert "/claims/{claim_id}/veracity" in paths


def test_router_meta() -> None:
    from clipos_api.research import router

    assert router.tags == ["research"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def test_route_packet_claim_source_roundtrip(client) -> None:
    packet_response = client.post(
        "/research-packets",
        json={
            "research_packet_id": "packet-1",
            "org_id": "org-1",
            "title": "Fixture packet",
            "summary": "A short summary",
        },
    )
    assert packet_response.status_code == 200
    assert packet_response.json()["research_packet_id"] == "packet-1"

    claim_response = client.post(
        "/research-packets/packet-1/claims",
        json={
            "claim_id": "claim-1",
            "org_id": "org-1",
            "research_packet_id": "packet-1",
            "statement": "The fixture claim statement",
        },
    )
    assert claim_response.status_code == 200
    assert claim_response.json()["veracity"] == "UNVERIFIED"

    source_response = client.post(
        "/claims/claim-1/sources",
        json={"org_id": "org-1", "source_ref": "https://example.test/a"},
    )
    assert source_response.status_code == 200
    assert source_response.json()["support"] == "SUPPORTS"

    listed = client.get("/claims/claim-1/sources", params={"org_id": "org-1"})
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    claims = client.get("/research-packets/packet-1/claims", params={"org_id": "org-1"})
    assert claims.status_code == 200
    assert len(claims.json()) == 1


def test_route_veracity_roundtrip(client) -> None:
    _register_packet_http(client)
    _register_claim_http(client)
    response = client.post(
        "/claims/claim-1/veracity",
        json={
            "org_id": "org-1",
            "veracity": "VERIFIED",
            "actor_ref": "reviewer-1",
            "reason_ref": "checked twice",
        },
    )
    assert response.status_code == 200
    assert response.json()["veracity"] == "VERIFIED"

    fetched = client.get("/research-packets/packet-1/claims", params={"org_id": "org-1"})
    assert fetched.json()[0]["veracity"] == "VERIFIED"


def _register_packet_http(client) -> None:
    response = client.post(
        "/research-packets",
        json={
            "research_packet_id": "packet-1",
            "org_id": "org-1",
            "title": "Fixture packet",
            "summary": "A short summary",
        },
    )
    assert response.status_code == 200


def _register_claim_http(client) -> None:
    response = client.post(
        "/research-packets/packet-1/claims",
        json={
            "claim_id": "claim-1",
            "org_id": "org-1",
            "research_packet_id": "packet-1",
            "statement": "The fixture claim statement",
        },
    )
    assert response.status_code == 200


def test_route_packet_missing_maps_to_404(client) -> None:
    response = client.get("/research-packets/missing", params={"org_id": "org-1"})
    assert response.status_code == 404
    claims = client.get("/research-packets/missing/claims", params={"org_id": "org-1"})
    assert claims.status_code == 404


def test_route_packet_conflict_maps_to_409(client) -> None:
    _register_packet_http(client)
    response = client.post(
        "/research-packets",
        json={
            "research_packet_id": "packet-1",
            "org_id": "org-1",
            "title": "Different title",
            "summary": "A short summary",
        },
    )
    assert response.status_code == 409


def test_route_claim_path_mismatch_maps_to_422(client) -> None:
    _register_packet_http(client)
    response = client.post(
        "/research-packets/packet-1/claims",
        json={
            "claim_id": "claim-1",
            "org_id": "org-1",
            "research_packet_id": "other-packet",
            "statement": "The fixture claim statement",
        },
    )
    assert response.status_code == 422


def test_route_link_source_missing_claim_maps_to_404(client) -> None:
    response = client.post(
        "/claims/missing/sources",
        json={"org_id": "org-1", "source_ref": "https://example.test/a"},
    )
    assert response.status_code == 404


def test_route_veracity_missing_claim_maps_to_404(client) -> None:
    response = client.post(
        "/claims/missing/veracity",
        json={"org_id": "org-1", "veracity": "VERIFIED", "actor_ref": "r"},
    )
    assert response.status_code == 404


def test_route_list_packets(client) -> None:
    _register_packet_http(client)
    listing = client.get("/research-packets", params={"org_id": "org-1"})
    assert listing.status_code == 200
    assert len(listing.json()) == 1
    assert listing.json()[0]["research_packet_id"] == "packet-1"
