"""Tests for the licensed stock/b-roll adapter interface (CLIP-056)."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.rights_entities import RightsEntitiesStore, SourceItemRecord
from clipos_api.stock_adapter import (
    MockStockClipAdapter,
    StockSearchRequest,
    list_stock_items,
    search_stock,
    stock_creator_id,
    stock_source_id,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-056-stock-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def rights(engine: Engine, audit_log: Path) -> RightsEntitiesStore:
    return RightsEntitiesStore.from_engine(engine)


def _request(**overrides: object) -> StockSearchRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "query": "sunlit kitchen b-roll",
        "provider_alias": "stock-provider-v1",
    }
    payload.update(overrides)
    return StockSearchRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


def test_mock_search_deterministic() -> None:
    adapter = MockStockClipAdapter()
    first = adapter.search("ocean waves", "stock-provider-v1", 5)
    second = adapter.search("ocean waves", "stock-provider-v1", 5)
    assert [item.model_dump() for item in first] == [item.model_dump() for item in second]
    assert len(first) == 3


def test_mock_search_normalizes_query_whitespace() -> None:
    adapter = MockStockClipAdapter()
    first = adapter.search("ocean   waves", "stock-provider-v1", 5)
    second = adapter.search("ocean waves", "stock-provider-v1", 5)
    assert first[0].provider_ref == second[0].provider_ref


def test_mock_search_respects_limit_and_licenses() -> None:
    adapter = MockStockClipAdapter()
    limited = adapter.search("desk", "stock-provider-v1", 2)
    assert len(limited) == 2
    assert all(item.license_ref == "CC0-1.0" for item in limited)
    assert all(item.width == 1920 and item.height == 1080 for item in limited)


def test_mock_search_differs_by_provider_alias() -> None:
    adapter = MockStockClipAdapter()
    a = adapter.search("desk", "provider-a", 5)
    b = adapter.search("desk", "provider-b", 5)
    assert a[0].provider_ref != b[0].provider_ref


# ---------------------------------------------------------------------------
# Deterministic id helpers
# ---------------------------------------------------------------------------


def test_stock_ids_embed_org() -> None:
    creator = stock_creator_id("org-1", "stock-provider-v1")
    source = stock_source_id("org-1", "stock-provider-v1")
    assert creator.startswith("org-1:")
    assert source.startswith("org-1:")
    assert creator != source
    assert len(creator) <= 128 and len(source) <= 128


# ---------------------------------------------------------------------------
# Search flow (rights-entity integration)
# ---------------------------------------------------------------------------


def test_search_registers_creator_and_source(rights: RightsEntitiesStore) -> None:
    search_stock(MockStockClipAdapter(), rights, _request())
    creator_id = stock_creator_id("org-1", "stock-provider-v1")
    source_id = stock_source_id("org-1", "stock-provider-v1")
    creator = rights.get_creator(creator_id)
    assert creator is not None
    assert creator.org_id == "org-1"
    source = rights.get_source(source_id)
    assert source is not None
    assert source.creator_id == creator_id
    assert source.source_type.value == "partner_feed"


def test_search_persists_and_is_idempotent(rights: RightsEntitiesStore, audit_log: Path) -> None:
    first = search_stock(MockStockClipAdapter(), rights, _request())
    second = search_stock(MockStockClipAdapter(), rights, _request())
    assert len(first) == 3
    assert [item.source_item_id for item in first] == [item.source_item_id for item in second]
    source_id = stock_source_id("org-1", "stock-provider-v1")
    assert len(rights.list_items(source_id, "org-1")) == 3
    log = audit_log.read_text(encoding="utf-8")
    assert "stock_searches.completed" in log
    assert log.count("stock_searches.completed") == 2
    assert "rights_entities.item_registered" in log


def test_search_limit_clamped(rights: RightsEntitiesStore) -> None:
    items = search_stock(MockStockClipAdapter(), rights, _request(limit=1))
    assert len(items) == 1


def test_search_changed_provider_metadata_conflicts(
    rights: RightsEntitiesStore, engine: Engine
) -> None:
    search_stock(MockStockClipAdapter(), rights, _request(limit=1))
    source_id = stock_source_id("org-1", "stock-provider-v1")
    stored = rights.list_items(source_id, "org-1")
    assert len(stored) == 1
    with rights._sessions.begin() as session:
        row = session.scalars(
            sa.select(SourceItemRecord).where(SourceItemRecord.item_id == stored[0].item_id)
        ).first()
        assert row is not None
        metadata = dict(row.item_metadata or {})
        metadata["license_ref"] = "Proprietary"
        row.item_metadata = metadata
        session.flush()
    with pytest.raises(ValueError, match="already registered with different fields"):
        search_stock(MockStockClipAdapter(), rights, _request(limit=1))


def test_list_items_scoped(rights: RightsEntitiesStore) -> None:
    search_stock(MockStockClipAdapter(), rights, _request())
    items = list_stock_items(rights, "org-1", "stock-provider-v1")
    assert len(items) == 3
    assert all(item.license_ref == "CC0-1.0" for item in items)
    assert all(item.provider_alias == "stock-provider-v1" for item in items)
    assert all(item.title for item in items)
    # source ids embed the org, so a foreign org derives a different source
    with pytest.raises(ValueError, match="source not found"):
        list_stock_items(rights, "org-other", "stock-provider-v1")


def test_list_items_missing_provider_source_raises(rights: RightsEntitiesStore) -> None:
    with pytest.raises(ValueError, match="source not found"):
        list_stock_items(rights, "org-1", "never-searched")


def test_provider_alias_required_by_pydantic() -> None:
    with pytest.raises(ValidationError):
        StockSearchRequest(org_id="org-1", query="waves", provider_alias="")


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/stock-searches" in paths
    assert "/stock-items" in paths


def test_router_meta() -> None:
    from clipos_api.stock_adapter import router

    assert router.tags == ["stock"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def test_route_search_and_list_roundtrip(client) -> None:
    response = client.post(
        "/stock-searches",
        json={
            "org_id": "org-1",
            "query": "sunlit kitchen b-roll",
            "provider_alias": "stock-provider-v1",
            "limit": 5,
        },
    )
    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 3
    assert all(item["license_ref"] == "CC0-1.0" for item in items)
    assert items[0]["source_id"].startswith("org-1:")

    listing = client.get(
        "/stock-items",
        params={"org_id": "org-1", "provider_alias": "stock-provider-v1"},
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 3

    # repeat search -> same rows, no growth
    again = client.post(
        "/stock-searches",
        json={
            "org_id": "org-1",
            "query": "sunlit kitchen b-roll",
            "provider_alias": "stock-provider-v1",
            "limit": 5,
        },
    )
    assert again.status_code == 200
    assert (
        len(
            client.get(
                "/stock-items",
                params={"org_id": "org-1", "provider_alias": "stock-provider-v1"},
            ).json()
        )
        == 3
    )


def test_route_list_never_searched_maps_to_404(client) -> None:
    response = client.get("/stock-items", params={"org_id": "org-1", "provider_alias": "never"})
    assert response.status_code == 404


def test_route_invalid_payload_maps_to_422(client) -> None:
    response = client.post(
        "/stock-searches",
        json={"org_id": "org-1", "query": "x", "provider_alias": ""},
    )
    assert response.status_code == 422
