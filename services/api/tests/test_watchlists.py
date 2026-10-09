"""Tests for watchlists, summary, alerts, and Trend workflow (CLIP-037)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.watchlists import (
    RunStatus,
    TrendWorkflowRun,
    TrendWorkflowRunner,
    WatchlistEntry,
    WatchlistSnapshot,
    WatchlistStore,
    watchlist_summary,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base
    from clipos_api.watchlists import (  # noqa: F401
        WatchlistAlertRecord,
        WatchlistEntryRecord,
        WatchlistRunRecord,
    )

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-037-watchlists-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> WatchlistStore:
    return WatchlistStore.from_engine(engine)


def _entry(watchlist_id: str = "wl-1", org_id: str = "org-1", **changes: object) -> WatchlistEntry:
    values = {
        "watchlist_id": watchlist_id,
        "org_id": org_id,
        "topic_id": "ai-video",
        "threshold": 0.8,
    }
    values.update(changes)
    return WatchlistEntry.model_validate(values)


def _snapshot(topic_id: str = "ai-video", value: float = 0.9) -> WatchlistSnapshot:
    return WatchlistSnapshot(topic_id=topic_id, metric_name="momentum", metric_value=value)


# ----------------------------------------------------------------------
# Summary purity
# ----------------------------------------------------------------------


def test_summary_breach_and_ok() -> None:
    entries = [_entry("wl-1", threshold=0.8), _entry("wl-2", topic_id="other", threshold=0.5)]
    snapshots = {"ai-video": _snapshot("ai-video", 0.9), "other": _snapshot("other", 0.3)}
    rows = watchlist_summary(entries, snapshots)
    by_topic = {r.topic_id: r for r in rows}
    assert by_topic["ai-video"].breached is True
    assert by_topic["other"].breached is False


def test_summary_no_snapshot_breaches() -> None:
    rows = watchlist_summary([_entry("wl-1")], {})
    assert rows[0].breached is True
    assert rows[0].metric_name is None


def test_summary_deterministic_order() -> None:
    entries = [_entry("wl-b", topic_id="z"), _entry("wl-a", topic_id="a")]
    first = watchlist_summary(entries, {})
    second = watchlist_summary(list(reversed(entries)), {})
    assert first == second


# ----------------------------------------------------------------------
# Entries
# ----------------------------------------------------------------------


def test_register_entry_idempotent(store: WatchlistStore) -> None:
    first = store.register_entry(_entry())
    again = store.register_entry(_entry())
    assert first == again


def test_register_entry_conflict_raises(store: WatchlistStore) -> None:
    store.register_entry(_entry())
    with pytest.raises(ValueError, match="different fields"):
        store.register_entry(_entry(threshold=0.1))


def test_entries_org_isolated(store: WatchlistStore) -> None:
    store.register_entry(_entry("wl-1", "org-1"))
    store.register_entry(_entry("wl-1", "org-2", topic_id="x"))
    assert len(store.list_entries("org-1")) == 1
    assert len(store.list_entries("org-2")) == 1


def test_list_entries_active_filter(store: WatchlistStore) -> None:
    store.register_entry(_entry("wl-1"))
    store.register_entry(_entry("wl-2", active=False))
    assert [e.watchlist_id for e in store.list_entries("org-1")] == ["wl-1"]
    assert len(store.list_entries("org-1", active_only=False)) == 2


# ----------------------------------------------------------------------
# Workflow runs
# ----------------------------------------------------------------------


def test_run_succeeds_and_persists_alerts(store: WatchlistStore) -> None:
    store.register_entry(_entry("wl-1", threshold=0.8))
    runner = TrendWorkflowRunner(store)
    run = TrendWorkflowRun(run_id="run-1", org_id="org-1")
    finished, alerts = runner.execute(run, {"ai-video": _snapshot(value=0.9)})
    assert finished.status is RunStatus.SUCCEEDED
    assert finished.finished_at is not None
    assert [a.topic_id for a in alerts] == ["ai-video"]
    assert alerts[0].metric_value == pytest.approx(0.9)


def test_run_no_breach_no_alerts(store: WatchlistStore) -> None:
    store.register_entry(_entry("wl-1", threshold=0.95))
    runner = TrendWorkflowRunner(store)
    finished, alerts = runner.execute(
        TrendWorkflowRun(run_id="run-2", org_id="org-1"),
        {"ai-video": _snapshot(value=0.5)},
    )
    assert finished.status is RunStatus.SUCCEEDED
    assert alerts == []


def test_run_idempotent_alerts(store: WatchlistStore) -> None:
    store.register_entry(_entry("wl-1", threshold=0.8))
    runner = TrendWorkflowRunner(store)
    runner.execute(TrendWorkflowRun(run_id="run-3", org_id="org-1"), {"ai-video": _snapshot()})
    # Same run + same data -> no-op, no duplicate alerts.
    _finished, alerts = runner.execute(
        TrendWorkflowRun(run_id="run-3", org_id="org-1"), {"ai-video": _snapshot()}
    )
    assert len(alerts) == 1


def test_run_conflicting_data_raises(store: WatchlistStore) -> None:
    store.register_entry(_entry("wl-1", threshold=0.8))
    runner = TrendWorkflowRunner(store)
    runner.execute(TrendWorkflowRun(run_id="run-4", org_id="org-1"), {"ai-video": _snapshot()})
    with pytest.raises(ValueError, match="different fields"):
        runner.execute(
            TrendWorkflowRun(run_id="run-4", org_id="org-1"), {"ai-video": _snapshot(value=0.7)}
        )


def test_inactive_entries_skipped(store: WatchlistStore) -> None:
    store.register_entry(_entry("wl-1", active=False))
    runner = TrendWorkflowRunner(store)
    finished, alerts = runner.execute(
        TrendWorkflowRun(run_id="run-5", org_id="org-1"), {"ai-video": _snapshot()}
    )
    assert finished.status is RunStatus.SUCCEEDED
    assert alerts == []


# ----------------------------------------------------------------------
# Alerts + audit
# ----------------------------------------------------------------------


def test_alerts_listed_org_scoped(store: WatchlistStore) -> None:
    store.register_entry(_entry("wl-1", "org-1"))
    store.register_entry(_entry("wl-2", "org-2", topic_id="t2"))
    runner = TrendWorkflowRunner(store)
    runner.execute(TrendWorkflowRun(run_id="r1", org_id="org-1"), {"ai-video": _snapshot()})
    runner.execute(TrendWorkflowRun(run_id="r2", org_id="org-2"), {"t2": _snapshot("t2", 1.0)})
    assert len(store.list_alerts("org-1")) == 1
    assert len(store.list_alerts("org-2")) == 1


def test_store_writes_audit_events(store: WatchlistStore) -> None:
    store.register_entry(_entry())
    runner = TrendWorkflowRunner(store)
    runner.execute(TrendWorkflowRun(run_id="run-a", org_id="org-1"), {"ai-video": _snapshot()})
    actions = [e["action"] for e in audit.read_events()]
    assert "watchlists.entry_registered" in actions
    assert "watchlists.run_started" in actions
    assert "watchlists.alert_recorded" in actions
    assert "watchlists.run_succeeded" in actions


def test_run_failure_audited(store: WatchlistStore) -> None:
    store.register_entry(_entry("wl-1", threshold=0.8))
    runner = TrendWorkflowRunner(store)
    runner.execute(TrendWorkflowRun(run_id="run-6", org_id="org-1"), {"ai-video": _snapshot()})
    # Same run but conflicting snapshot -> record_alert raises -> FAILED audited.
    with pytest.raises(ValueError, match="different fields"):
        runner.execute(
            TrendWorkflowRun(run_id="run-6", org_id="org-1"),
            {"ai-video": _snapshot(value=0.2)},
        )
    actions = [e["action"] for e in audit.read_events()]
    assert "watchlists.run_failed" in actions


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


def test_route_register_conflict_maps_to_409(store: WatchlistStore) -> None:
    from clipos_api.watchlists import register_watchlist_entry_route

    store.register_entry(_entry())
    with pytest.raises(HTTPException) as err:
        register_watchlist_entry_route(_entry(threshold=0.2), store)
    assert err.value.status_code == 409


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/watchlists" in paths
    assert "/watchlists/summary" in paths
    assert "/trend-runs" in paths
    assert "/watchlists/alerts" in paths
