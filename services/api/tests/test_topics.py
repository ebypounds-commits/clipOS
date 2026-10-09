"""Tests for topics, topic aliases, and trend snapshots (CLIP-035)."""

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
from clipos_api.topics import (
    Topic,
    TopicAlias,
    TopicStore,
    TrendSnapshot,
    cluster_aliases,
    normalize_topic_text,
    router,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base
    from clipos_api.topics import TopicAliasRecord, TopicRecord, TrendSnapshotRecord  # noqa: F401

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-035-topics-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> TopicStore:
    return TopicStore.from_engine(engine)


def _topic(topic_id: str = "ai-video", org_id: str = "org-1", **changes: object) -> Topic:
    values = {
        "topic_id": topic_id,
        "org_id": org_id,
        "display_label": "AI Video Tools",
        "config": {"vertical": "tech"},
    }
    values.update(changes)
    return Topic.model_validate(values)


def _alias(
    alias: str = "aivideo", topic_id: str = "ai-video", org_id: str = "org-1", **changes: object
) -> TopicAlias:
    values = {"alias": alias, "org_id": org_id, "topic_id": topic_id}
    values.update(changes)
    return TopicAlias.model_validate(values)


def _snapshot(
    snapshot_id: str = "snap-1",
    topic_id: str = "ai-video",
    org_id: str = "org-1",
    **changes: object,
) -> TrendSnapshot:
    values = {
        "snapshot_id": snapshot_id,
        "topic_id": topic_id,
        "org_id": org_id,
        "metrics": {"volume": 120.0, "momentum": 0.35},
        "source_ref": "storage://signals/snap-1",
        "captured_at": datetime(2026, 10, 9, tzinfo=UTC),
    }
    values.update(changes)
    return TrendSnapshot.model_validate(values)


# ----------------------------------------------------------------------
# Topics
# ----------------------------------------------------------------------


def test_register_topic_is_idempotent(store: TopicStore) -> None:
    first = store.register_topic(_topic())
    again = store.register_topic(_topic())
    assert first == again
    assert first.created_at is not None


def test_register_topic_conflict_raises(store: TopicStore) -> None:
    store.register_topic(_topic())
    with pytest.raises(ValueError, match="different fields"):
        store.register_topic(_topic(display_label="Other Label"))


def test_topic_id_validator() -> None:
    with pytest.raises(ValueError, match="lowercase identifier"):
        _topic(topic_id="AI Video!")


def test_get_topic_roundtrip(store: TopicStore) -> None:
    registered = store.register_topic(_topic())
    stored = store.get_topic("ai-video", "org-1")
    assert stored == registered
    assert store.get_topic("missing", "org-1") is None


def test_topics_org_isolated_same_id(store: TopicStore) -> None:
    store.register_topic(_topic("topic-a", "org-1"))
    store.register_topic(_topic("topic-a", "org-2", display_label="Other"))
    assert len(store.list_topics("org-1")) == 1
    assert len(store.list_topics("org-2")) == 1
    assert store.get_topic("topic-a", "org-2").display_label == "Other"


# ----------------------------------------------------------------------
# Aliases
# ----------------------------------------------------------------------


def test_register_alias_requires_existing_topic(store: TopicStore) -> None:
    with pytest.raises(ValueError, match="topic not found"):
        store.register_alias(_alias())


def test_register_alias_idempotent(store: TopicStore) -> None:
    store.register_topic(_topic())
    first = store.register_alias(_alias())
    again = store.register_alias(_alias())
    assert first == again


def test_alias_conflict_same_alias_different_topic_raises(store: TopicStore) -> None:
    store.register_topic(_topic("topic-a"))
    store.register_topic(_topic("topic-b"))
    store.register_alias(_alias("shared-alias", "topic-a"))
    with pytest.raises(ValueError, match="different fields"):
        store.register_alias(_alias("shared-alias", "topic-b"))


def test_same_alias_in_two_orgs_allowed(store: TopicStore) -> None:
    store.register_topic(_topic("topic-a", "org-1"))
    store.register_topic(_topic("topic-a", "org-2"))
    a1 = store.register_alias(_alias("shared", "topic-a", "org-1"))
    a2 = store.register_alias(_alias("shared", "topic-a", "org-2"))
    assert a1.org_id != a2.org_id


def test_normalize_topic_text_pure() -> None:
    assert normalize_topic_text("  AI Video  Tools!  ") == "ai-video-tools"
    assert normalize_topic_text("AI_VIDEO") == "ai-video"
    assert normalize_topic_text("") == ""


def test_resolve_alias_normalizes(store: TopicStore) -> None:
    store.register_topic(_topic())
    store.register_alias(_alias("ai-video-tools", "ai-video"))
    resolved = store.resolve_alias("AI  Video  Tools!", "org-1")
    assert resolved is not None
    assert resolved.topic_id == "ai-video"
    assert store.resolve_alias("unrelated thing", "org-1") is None


def test_cluster_aliases_deterministic_and_groups_similar() -> None:
    aliases = ["AI Video Tools", "ai-video-tools", "Unrelated Thing", "video tools"]
    first = cluster_aliases(aliases)
    second = cluster_aliases(list(reversed(aliases)))
    assert first == second
    flat = {a for c in first for a in c}
    assert flat == {normalize_topic_text(a) for a in aliases if normalize_topic_text(a)}
    # distinct normalized forms stay separate; identical ones merge
    merged = cluster_aliases(["AI Video Tools", "ai-video-tools"])
    assert len(merged) == 1 and len(merged[0]) == 2


# ----------------------------------------------------------------------
# Trend snapshots (append-only)
# ----------------------------------------------------------------------


def test_snapshot_requires_existing_topic(store: TopicStore) -> None:
    with pytest.raises(ValueError, match="topic not found"):
        store.append_snapshot(_snapshot())


def test_snapshot_idempotent(store: TopicStore) -> None:
    store.register_topic(_topic())
    first = store.append_snapshot(_snapshot())
    again = store.append_snapshot(_snapshot())
    assert first == again


def test_snapshot_conflict_raises(store: TopicStore) -> None:
    store.register_topic(_topic())
    store.append_snapshot(_snapshot())
    with pytest.raises(ValueError, match="different fields"):
        store.append_snapshot(_snapshot(metrics={"volume": 999.0}))


def test_snapshots_are_append_many_ordered(store: TopicStore) -> None:
    store.register_topic(_topic())
    store.append_snapshot(_snapshot("snap-2", captured_at=datetime(2026, 10, 10, tzinfo=UTC)))
    store.append_snapshot(_snapshot("snap-1", captured_at=datetime(2026, 10, 9, tzinfo=UTC)))
    listed = store.list_snapshots("org-1")
    assert [s.snapshot_id for s in listed] == ["snap-1", "snap-2"]


def test_snapshots_filter_by_topic(store: TopicStore) -> None:
    store.register_topic(_topic("topic-a"))
    store.register_topic(_topic("topic-b"))
    store.append_snapshot(_snapshot("snap-a", topic_id="topic-a"))
    store.append_snapshot(_snapshot("snap-b", topic_id="topic-b"))
    assert [s.snapshot_id for s in store.list_snapshots("org-1", "topic-a")] == ["snap-a"]
    assert len(store.list_snapshots("org-1")) == 2


# ----------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------


def test_store_writes_audit_events(store: TopicStore) -> None:
    store.register_topic(_topic())
    store.register_alias(_alias())
    store.append_snapshot(_snapshot())
    events = audit.read_events()
    actions = [e["action"] for e in events]
    assert "topics.topic_registered" in actions
    assert "topics.alias_registered" in actions
    assert "topics.snapshot_appended" in actions


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


def test_route_register_topic_conflict_maps_to_409(store: TopicStore) -> None:
    from clipos_api.topics import register_topic_route

    store.register_topic(_topic())
    with pytest.raises(HTTPException) as err:
        register_topic_route(_topic(config={}), store)
    assert err.value.status_code == 409


def test_route_get_missing_topic_maps_to_404(store: TopicStore) -> None:
    from clipos_api.topics import get_topic_route

    with pytest.raises(HTTPException) as err:
        get_topic_route("missing", store, "org-1")
    assert err.value.status_code == 404


def test_route_alias_path_mismatch_maps_to_422(store: TopicStore) -> None:
    from clipos_api.topics import register_topic_alias_route

    store.register_topic(_topic())
    with pytest.raises(HTTPException) as err:
        register_topic_alias_route(_alias(topic_id="other"), "ai-video", store)
    assert err.value.status_code == 422


def test_route_snapshot_not_found_maps_to_404(store: TopicStore) -> None:
    from clipos_api.topics import append_trend_snapshot_route

    with pytest.raises(HTTPException) as err:
        append_trend_snapshot_route(_snapshot(), "ai-video", store)
    assert err.value.status_code == 404


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/topics" in paths
    assert "/topics/{topic_id}" in paths
    assert "/topics/{topic_id}/aliases" in paths
    assert "/topics/{topic_id}/snapshots" in paths


def test_router_meta() -> None:
    assert router.tags == ["topics"]
