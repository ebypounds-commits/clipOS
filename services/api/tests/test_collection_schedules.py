"""Tests for scheduled collection (CLIP-065)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.analytics import AnalyticsStore, MockAnalyticsAdapter
from clipos_api.collection_schedules import (
    CollectionScheduleStore,
    RegisterScheduleRequest,
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
    path = Path("/tmp") / "clipos-065-schedules-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> CollectionScheduleStore:
    return CollectionScheduleStore.from_engine(engine)


@pytest.fixture()
def analytics_store(engine: Engine) -> AnalyticsStore:
    return AnalyticsStore.from_engine(engine)


def _request(**overrides: object) -> RegisterScheduleRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "platform": "youtube",
        "account_ref": "yt-1",
        "content_ref": "content://vid.mp4",
        "external_id": "yt-mock-abc",
        "interval_minutes": 60,
    }
    payload.update(overrides)
    return RegisterScheduleRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Store: register
# ---------------------------------------------------------------------------


def test_register_idempotent(store: CollectionScheduleStore) -> None:
    first = store.register_schedule(_request())
    second = store.register_schedule(_request())
    assert first.schedule_id == second.schedule_id
    assert len(store.list_schedules("org-1")) == 1


def test_register_conflict_changed_fields(store: CollectionScheduleStore) -> None:
    store.register_schedule(_request())
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_schedule(_request(interval_minutes=30))


def test_register_sets_next_run(store: CollectionScheduleStore) -> None:
    schedule = store.register_schedule(_request())
    assert schedule.next_run_at is not None
    assert schedule.last_run_at is None
    assert schedule.enabled is True
    assert schedule.next_run_at > datetime.now(UTC) - timedelta(minutes=1)


def test_register_records_audit(
    store: CollectionScheduleStore,
    audit_log: Path,
) -> None:
    store.register_schedule(_request())
    assert "collection_schedules.registered" in audit_log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Store: list
# ---------------------------------------------------------------------------


def test_list_org_and_enabled_filter(store: CollectionScheduleStore) -> None:
    store.register_schedule(_request())
    store.register_schedule(
        _request(
            content_ref="content://other.mp4",
            external_id="yt-mock-def",
            enabled=False,
        )
    )
    assert len(store.list_schedules("org-1")) == 2
    assert len(store.list_schedules("org-1", enabled=True)) == 1
    assert len(store.list_schedules("org-1", enabled=False)) == 1
    assert len(store.list_schedules("org-other")) == 0


# ---------------------------------------------------------------------------
# Store: run_due
# ---------------------------------------------------------------------------


def _make_due(store: CollectionScheduleStore, engine: Engine, schedule_id: str) -> None:
    import sqlalchemy as sa

    from clipos_api.collection_schedules import CollectionScheduleRecord

    past = datetime.now(UTC) - timedelta(minutes=5)
    with engine.begin() as conn:
        conn.execute(
            sa.update(CollectionScheduleRecord)
            .where(CollectionScheduleRecord.schedule_id == schedule_id)
            .values(next_run_at=past)
        )


def test_run_due_not_due_returns_zero(
    store: CollectionScheduleStore,
    analytics_store: AnalyticsStore,
) -> None:
    store.register_schedule(_request())
    result = store.run_due("org-1", MockAnalyticsAdapter(), analytics_store)
    assert result.ran == 0


def test_run_due_collects_and_advances(
    store: CollectionScheduleStore,
    analytics_store: AnalyticsStore,
    engine: Engine,
    audit_log: Path,
) -> None:
    schedule = store.register_schedule(_request())
    _make_due(store, engine, schedule.schedule_id)

    result = store.run_due("org-1", MockAnalyticsAdapter(), analytics_store)
    assert result.ran == 1
    outcome = result.outcomes[0]
    assert outcome.status == "collected"
    assert outcome.metric_count == 3

    stored = analytics_store.get_metrics("org-1", "content://vid.mp4")
    assert len(stored) == 3

    updated = store.list_schedules("org-1")[0]
    assert updated.last_run_at is not None
    assert updated.next_run_at is not None
    assert updated.next_run_at > datetime.now(UTC)
    assert "collection_schedules.executed" in audit_log.read_text(encoding="utf-8")


def test_run_due_skips_disabled(
    store: CollectionScheduleStore,
    analytics_store: AnalyticsStore,
    engine: Engine,
) -> None:
    schedule = store.register_schedule(_request(enabled=False))
    _make_due(store, engine, schedule.schedule_id)
    result = store.run_due("org-1", MockAnalyticsAdapter(), analytics_store)
    assert result.ran == 0


def test_run_due_adapter_failure_advances_schedule(
    store: CollectionScheduleStore,
    analytics_store: AnalyticsStore,
    engine: Engine,
) -> None:
    schedule = store.register_schedule(_request())
    _make_due(store, engine, schedule.schedule_id)

    class FailingAdapter:
        def health_check(self):
            raise NotImplementedError

        def collect_metrics(self, account_ref, content_ref, external_id):
            raise RuntimeError("upstream metrics API unavailable")

    result = store.run_due("org-1", FailingAdapter(), analytics_store)  # type: ignore[arg-type]
    assert result.ran == 1
    outcome = result.outcomes[0]
    assert outcome.status == "failed"
    assert "unavailable" in (outcome.error or "")

    updated = store.list_schedules("org-1")[0]
    assert updated.next_run_at is not None
    assert updated.next_run_at > datetime.now(UTC)


def test_run_due_org_scoped(
    store: CollectionScheduleStore,
    analytics_store: AnalyticsStore,
    engine: Engine,
) -> None:
    schedule = store.register_schedule(_request())
    _make_due(store, engine, schedule.schedule_id)
    result = store.run_due("org-other", MockAnalyticsAdapter(), analytics_store)
    assert result.ran == 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/collection-schedules" in paths
    assert "/collection-schedules/run-due" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_register_and_list_roundtrip(client) -> None:
    payload = {
        "org_id": "org-1",
        "platform": "youtube",
        "account_ref": "yt-1",
        "content_ref": "content://vid.mp4",
        "external_id": "yt-mock-abc",
        "interval_minutes": 60,
    }
    created = client.post("/collection-schedules", json=payload)
    assert created.status_code == 200, created.text

    listing = client.get("/collection-schedules", params={"org_id": "org-1"})
    assert listing.status_code == 200
    assert len(listing.json()) == 1

    conflict = client.post("/collection-schedules", json={**payload, "interval_minutes": 30})
    assert conflict.status_code == 409


def test_run_due_route_roundtrip(engine: Engine, client) -> None:
    payload = {
        "org_id": "org-1",
        "platform": "youtube",
        "account_ref": "yt-1",
        "content_ref": "content://vid.mp4",
        "external_id": "yt-mock-abc",
        "interval_minutes": 60,
    }
    created = client.post("/collection-schedules", json=payload)
    schedule_id = created.json()["schedule_id"]

    import sqlalchemy as sa

    from clipos_api.collection_schedules import CollectionScheduleRecord

    past = datetime.now(UTC) - timedelta(minutes=5)
    with engine.begin() as conn:
        conn.execute(
            sa.update(CollectionScheduleRecord)
            .where(CollectionScheduleRecord.schedule_id == schedule_id)
            .values(next_run_at=past)
        )

    resp = client.post("/collection-schedules/run-due", json={"org_id": "org-1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ran"] == 1
    assert body["outcomes"][0]["status"] == "collected"

    # Second call: schedule was advanced -> nothing due.
    again = client.post("/collection-schedules/run-due", json={"org_id": "org-1"})
    assert again.json()["ran"] == 0
