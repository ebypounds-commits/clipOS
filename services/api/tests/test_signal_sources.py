"""Tests for signal sources and observations (CLIP-034)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.signal_sources import (
    SignalObservation,
    SignalSource,
    SignalSourcesStore,
    router,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.signal_sources import SignalObservationRecord, SignalSourceRecord  # noqa: F401

    Base.metadata.create_all(e)
    return e


from clipos_api.repository import Base


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-034-signal-sources-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> SignalSourcesStore:
    return SignalSourcesStore.from_engine(engine)


def _source(source_id: str = "src-1", org_id: str = "org-1", **changes: object) -> SignalSource:
    values = {
        "source_id": source_id,
        "org_id": org_id,
        "source_type": "rss",
        "canonical_url": "https://example.com/feed.xml",
        "config": {"poll_seconds": 300},
    }
    values.update(changes)
    return SignalSource.model_validate(values)


def _observation(
    observation_id: str = "obs-1",
    source_id: str = "src-1",
    org_id: str = "org-1",
    **changes: object,
) -> SignalObservation:
    values = {
        "observation_id": observation_id,
        "source_id": source_id,
        "org_id": org_id,
        "payload_ref": "storage://signals/obs-1",
        "captured_at": datetime(2026, 10, 8, tzinfo=UTC),
    }
    values.update(changes)
    return SignalObservation.model_validate(values)


def test_register_source_is_idempotent(store: SignalSourcesStore) -> None:
    first = store.register_source(_source())
    again = store.register_source(_source())
    assert first == again
    assert first.created_at is not None


def test_register_source_conflict_raises(store: SignalSourcesStore) -> None:
    store.register_source(_source())
    with pytest.raises(ValueError, match="different fields"):
        store.register_source(_source(config={"poll_seconds": 60}))


def test_source_type_validator(store: SignalSourcesStore) -> None:
    with pytest.raises(ValueError, match="lowercase identifier"):
        _source(source_type="RSS-Feed")


def test_get_source_roundtrip(store: SignalSourcesStore) -> None:
    registered = store.register_source(_source())
    stored = store.get_source("src-1")
    assert stored == registered
    assert store.get_source("missing") is None


def test_list_sources_org_scoped(store: SignalSourcesStore) -> None:
    store.register_source(_source("src-1", "org-1"))
    store.register_source(_source("src-2", "org-1"))
    store.register_source(_source("src-1", "org-2", canonical_url=None))
    org1 = store.list_sources("org-1")
    assert [s.source_id for s in org1] == ["src-1", "src-2"]
    assert all(s.org_id == "org-1" for s in org1)
    assert len(store.list_sources("org-2")) == 1


def test_observation_requires_existing_source(store: SignalSourcesStore) -> None:
    with pytest.raises(ValueError, match="signal source not found"):
        store.append_observation(_observation())


def test_observation_org_mismatch_raises(store: SignalSourcesStore) -> None:
    store.register_source(_source())
    with pytest.raises(ValueError, match="different organization"):
        store.append_observation(_observation(org_id="org-2"))


def test_append_observation_is_idempotent(store: SignalSourcesStore) -> None:
    store.register_source(_source())
    first = store.append_observation(_observation())
    again = store.append_observation(_observation())
    assert first == again
    assert len(store.list_observations("org-1")) == 1


def test_observation_conflict_raises(store: SignalSourcesStore) -> None:
    store.register_source(_source())
    store.append_observation(_observation())
    with pytest.raises(ValueError, match="different fields"):
        store.append_observation(_observation(payload_ref="storage://signals/other"))


def test_observations_are_append_only_ordered(store: SignalSourcesStore) -> None:
    store.register_source(_source())
    store.append_observation(_observation("obs-2", captured_at=datetime(2026, 10, 9, tzinfo=UTC)))
    store.append_observation(_observation("obs-1", captured_at=datetime(2026, 10, 8, tzinfo=UTC)))
    listed = store.list_observations("org-1")
    assert [o.observation_id for o in listed] == ["obs-1", "obs-2"]


def test_list_observations_filters_by_source(store: SignalSourcesStore) -> None:
    store.register_source(_source("src-1"))
    store.register_source(_source("src-2", canonical_url=None))
    store.append_observation(_observation("obs-1", source_id="src-1"))
    store.append_observation(_observation("obs-2", source_id="src-2"))
    assert [o.observation_id for o in store.list_observations("org-1", "src-1")] == ["obs-1"]
    assert len(store.list_observations("org-1")) == 2


def test_store_writes_audit_events(store: SignalSourcesStore) -> None:
    store.register_source(_source())
    store.append_observation(_observation())
    events = audit.read_events()
    actions = [e["action"] for e in events]
    assert "signal_sources.source_registered" in actions
    assert "signal_sources.observation_appended" in actions


def test_route_register_source_conflict_maps_to_409(
    store: SignalSourcesStore,
) -> None:
    store.register_source(_source())
    from clipos_api.signal_sources import register_signal_source_route

    with pytest.raises(HTTPException) as err:
        register_signal_source_route(_source(config={}), store)
    assert err.value.status_code == 409


def test_route_get_missing_maps_to_404(store: SignalSourcesStore) -> None:
    from clipos_api.signal_sources import get_signal_source_route

    with pytest.raises(HTTPException) as err:
        get_signal_source_route("missing", store)
    assert err.value.status_code == 404


def test_route_observation_path_mismatch_maps_to_422(
    store: SignalSourcesStore,
) -> None:
    store.register_source(_source())
    from clipos_api.signal_sources import append_signal_observation_route

    with pytest.raises(HTTPException) as err:
        append_signal_observation_route(_observation(source_id="other"), "src-1", store)
    assert err.value.status_code == 422


def test_route_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/signal-sources" in paths
    assert "/signal-sources/{source_id}" in paths
    assert "/signal-sources/{source_id}/observations" in paths


def test_router_meta() -> None:
    assert router.tags == ["signal-sources"]
