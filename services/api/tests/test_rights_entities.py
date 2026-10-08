"""Tests for rights & provenance foundational entities (CLIP-030)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit, rights_entities
from clipos_api.main import app
from clipos_api.rights_entities import (
    ContentSource,
    Creator,
    SourceItem,
    SourceType,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    rights_entities.Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-030-rights-entities-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> rights_entities.RightsEntitiesStore:
    return rights_entities.RightsEntitiesStore.from_engine(engine)


def _creator(creator_id: str = "creator-1", org_id: str = "org-1", **changes: object) -> Creator:
    values = {
        "creator_id": creator_id,
        "org_id": org_id,
        "display_name": "Ada Lovelace",
        "contact_ref": "secrets://contacts/creator-1",
        "metadata": {"locale": "en"},
    }
    values.update(changes)
    return Creator.model_validate(values)


def _source(source_id: str = "source-1", org_id: str = "org-1", **changes: object) -> ContentSource:
    values = {
        "source_id": source_id,
        "org_id": org_id,
        "creator_id": "creator-1",
        "source_type": SourceType.FEED,
        "canonical_url": "https://example.com/feed.xml",
        "active": True,
        "metadata": {"cadence": "hourly"},
    }
    values.update(changes)
    return ContentSource.model_validate(values)


def _item(item_id: str = "item-1", source_id: str = "source-1", **changes: object) -> SourceItem:
    values = {
        "item_id": item_id,
        "org_id": "org-1",
        "source_id": source_id,
        "external_ref": "yt:video:abc123",
        "captured_at": datetime(2026, 10, 8, tzinfo=UTC),
        "content_ref": "storage://source-items/abc123",
        "metadata": {"lang": "en"},
    }
    values.update(changes)
    return SourceItem.model_validate(values)


def test_creator_registration_is_idempotent_and_rejects_conflicts(
    store: rights_entities.RightsEntitiesStore,
) -> None:
    registered = store.register_creator(_creator())
    assert registered.created_at is not None
    assert store.register_creator(_creator()) == registered
    with pytest.raises(ValueError, match="different fields"):
        store.register_creator(_creator(display_name="Someone Else"))
    with pytest.raises(ValueError, match="different fields"):
        store.register_creator(_creator(contact_ref="secrets://contacts/other"))


def test_contact_ref_must_be_a_reference_not_inline() -> None:
    with pytest.raises(ValueError, match="secrets reference"):
        Creator.model_validate(
            {
                "creator_id": "creator-1",
                "org_id": "org-1",
                "display_name": "Ada",
                "contact_ref": "ada@example.com",
            }
        )


def test_source_registration_requires_existing_creator_and_matches_org(
    store: rights_entities.RightsEntitiesStore,
) -> None:
    with pytest.raises(ValueError, match="creator not found"):
        store.register_source(_source())
    store.register_creator(_creator())
    with pytest.raises(ValueError, match="different organization"):
        store.register_source(_source(org_id="org-2"))
    registered = store.register_source(_source())
    assert registered.active is True
    assert store.register_source(_source()) == registered
    with pytest.raises(ValueError, match="different fields"):
        store.register_source(_source(source_type=SourceType.UPLOAD))


def test_source_registration_is_idempotent_and_audited(
    store: rights_entities.RightsEntitiesStore, audit_log: Path
) -> None:
    store.register_creator(_creator())
    store.register_source(_source())
    store.register_source(_source())
    events = [event["action"] for event in audit.read_events()]
    assert events == [
        "rights_entities.creator_registered",
        "rights_entities.source_registered",
    ]


def test_source_item_registration_is_idempotent_on_external_ref(
    store: rights_entities.RightsEntitiesStore,
) -> None:
    store.register_creator(_creator())
    store.register_source(_source())
    registered = store.register_item(_item())
    assert registered.created_at is not None
    assert store.register_item(_item()) == registered
    with pytest.raises(ValueError, match="different fields"):
        store.register_item(_item(content_ref="storage://other"))


def test_source_item_requires_existing_source_and_matching_org(
    store: rights_entities.RightsEntitiesStore,
) -> None:
    with pytest.raises(ValueError, match="source not found"):
        store.register_item(_item())
    store.register_creator(_creator())
    store.register_source(_source())
    with pytest.raises(ValueError, match="different organization"):
        store.register_item(_item(org_id="org-2"))


def test_deactivation_is_audited_and_excludes_source_from_active_list(
    store: rights_entities.RightsEntitiesStore, audit_log: Path
) -> None:
    store.register_creator(_creator())
    store.register_source(_source("source-1"))
    store.register_source(_source("source-2"))
    deactivated = store.deactivate_source("source-1", "org-1")
    assert deactivated.active is False
    assert store.deactivate_source("source-1", "org-1").active is False
    active_ids = [s.source_id for s in store.list_sources("org-1", active_only=True)]
    assert active_ids == ["source-2"]
    all_ids = [s.source_id for s in store.list_sources("org-1")]
    assert all_ids == ["source-1", "source-2"]
    with pytest.raises(ValueError, match="not found"):
        store.deactivate_source("missing", "org-1")
    events = [event["action"] for event in audit.read_events()]
    assert events.count("rights_entities.source_deactivated") >= 1


def test_listings_are_org_scoped(store: rights_entities.RightsEntitiesStore) -> None:
    store.register_creator(_creator("creator-1", org_id="org-1"))
    store.register_creator(_creator("creator-2", org_id="org-2"))
    store.register_source(_source("source-1", org_id="org-1", creator_id="creator-1"))
    store.register_creator(_creator("creator-3", org_id="org-2"))
    store.register_source(
        _source("source-2", org_id="org-2", creator_id="creator-3", source_type=SourceType.UPLOAD)
    )
    assert [c.creator_id for c in store.list_creators("org-1")] == ["creator-1"]
    assert [c.creator_id for c in store.list_creators("org-2")] == ["creator-2", "creator-3"]
    assert [s.source_id for s in store.list_sources("org-1")] == ["source-1"]
    assert [s.source_id for s in store.list_sources("org-2")] == ["source-2"]
    store.register_item(_item("item-1", source_id="source-1", org_id="org-1"))
    assert [i.item_id for i in store.list_items("source-1", "org-1")] == ["item-1"]
    with pytest.raises(ValueError, match="not found"):
        store.list_items("source-1", "org-2")


def test_getters_return_registered_entities(
    store: rights_entities.RightsEntitiesStore,
) -> None:
    assert store.get_creator("missing") is None
    assert store.get_source("missing") is None
    store.register_creator(_creator())
    store.register_source(_source())
    assert store.get_creator("creator-1") is not None
    assert store.get_source("source-1") is not None


def test_routes_are_wired() -> None:
    names = {route.name for route in app.routes if isinstance(route, APIRoute)}
    assert {
        "register_creator",
        "get_creator",
        "register_source",
        "list_sources",
        "deactivate_source",
        "register_item",
        "list_items",
    } <= names


def test_no_provider_model_identifiers_in_module() -> None:
    src = Path(rights_entities.__file__).read_text()
    for provider_token in ("gpt-", "claude-", "gemini-", "deepseek", "qwen"):
        assert provider_token not in src, provider_token


def test_source_types_are_constrained() -> None:
    assert {member.value for member in SourceType} == {
        "upload",
        "feed",
        "partner_feed",
        "scrape_approved",
    }
    with pytest.raises(ValueError):
        ContentSource.model_validate(
            {
                "source_id": "s",
                "org_id": "o",
                "creator_id": "c",
                "source_type": "scrape_unapproved",
            }
        )
