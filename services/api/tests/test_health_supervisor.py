"""Tests for health supervisor signals (CLIP-086)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.health_supervisor import (
    HealthSignal,
    HealthStore,
    RecordSignalRequest,
    SignalStatus,
    run_live_probes,
    summarize_signals,
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
    path = Path("/tmp") / "clipos-086-health-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> HealthStore:
    return HealthStore.from_engine(engine)


def _signal(
    signal_id: str,
    subsystem: str,
    status: SignalStatus,
    created_at: datetime | None = None,
) -> HealthSignal:
    return HealthSignal(
        signal_id=signal_id,
        org_id="org-1",
        subsystem=subsystem,
        status=status,
        detail="",
        source="OPERATOR",
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Pure aggregation
# ---------------------------------------------------------------------------


def test_summarize_empty_is_unknown() -> None:
    summary = summarize_signals("org-1", [])
    assert summary.overall_status is SignalStatus.UNKNOWN
    assert summary.signals == []


def test_summarize_worst_rank_wins() -> None:
    signals = [
        _signal("s1", "database", SignalStatus.HEALTHY),
        _signal("s2", "cache", SignalStatus.DEGRADED),
        _signal("s3", "storage", SignalStatus.DOWN),
        _signal("s4", "workflow", SignalStatus.HEALTHY),
    ]
    summary = summarize_signals("org-1", signals)
    assert summary.overall_status is SignalStatus.DOWN
    assert len(summary.signals) == 4


def test_summarize_latest_per_subsystem() -> None:
    older = datetime.now(UTC) - timedelta(minutes=5)
    newer = datetime.now(UTC)
    signals = [
        _signal("old", "database", SignalStatus.DOWN, created_at=older),
        _signal("new", "database", SignalStatus.HEALTHY, created_at=newer),
    ]
    summary = summarize_signals("org-1", signals)
    assert len(summary.signals) == 1
    assert summary.signals[0].signal_id == "new"
    assert summary.overall_status is SignalStatus.HEALTHY


# ---------------------------------------------------------------------------
# Live probes
# ---------------------------------------------------------------------------


def test_live_probes_never_raise(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CACHE_REDIS_URL", raising=False)
    monkeypatch.delenv("STORAGE_ENDPOINT", raising=False)
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    results = run_live_probes(engine)
    assert len(results) == 4
    by_name = {r.subsystem: r for r in results}
    assert by_name["database"].status is SignalStatus.HEALTHY
    assert by_name["cache"].status is SignalStatus.UNKNOWN
    assert by_name["storage"].status is SignalStatus.UNKNOWN
    assert by_name["workflow"].status is SignalStatus.UNKNOWN


def test_cache_probe_degrades_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CACHE_REDIS_URL", "redis://127.0.0.1:1/0")
    from clipos_api.health_supervisor import _probe_cache

    result = _probe_cache()
    assert result.status is SignalStatus.DEGRADED


def test_storage_probe_degrades_on_bad_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_ENDPOINT", "http://127.0.0.1:1")
    from clipos_api.health_supervisor import _probe_storage

    result = _probe_storage()
    assert result.status is SignalStatus.DEGRADED


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _record(signal_id: str = "sig-1", **overrides: object) -> RecordSignalRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "signal_id": signal_id,
        "subsystem": "database",
        "status": SignalStatus.HEALTHY,
        "detail": "manual check",
        "source": "OPERATOR",
    }
    payload.update(overrides)
    return RecordSignalRequest(**payload)  # type: ignore[arg-type]


def test_record_idempotent_conflict_audit(
    store: HealthStore,
    audit_log: Path,
) -> None:
    first = store.record_signal(_record())
    again = store.record_signal(_record())
    assert first.signal_id == again.signal_id
    assert "health_signals.recorded" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.record_signal(_record(status=SignalStatus.DOWN))
    assert len(store.list_signals("org-1")) == 1


def test_record_probe_results_and_summarize(
    store: HealthStore,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CACHE_REDIS_URL", raising=False)
    monkeypatch.delenv("STORAGE_ENDPOINT", raising=False)
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    results = run_live_probes(engine)
    signals = store.record_probe_results("org-1", results)
    assert len(signals) == 4
    assert all(s.source == "LIVE" for s in signals)
    summary = store.summarize("org-1")
    # Unset env vars yield UNKNOWN, which outranks HEALTHY in the
    # worst-rank aggregation (DOWN > DEGRADED > UNKNOWN > HEALTHY).
    assert summary.overall_status is SignalStatus.UNKNOWN
    assert len(summary.signals) == 4
    by_name = {s.subsystem: s.status for s in summary.signals}
    assert by_name["database"] is SignalStatus.HEALTHY


def test_list_filter_by_subsystem(store: HealthStore) -> None:
    store.record_signal(_record("sig-1", subsystem="database"))
    store.record_signal(_record("sig-2", subsystem="cache"))
    assert len(store.list_signals("org-1")) == 2
    assert len(store.list_signals("org-1", "database")) == 1
    assert len(store.list_signals("org-other")) == 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/health/probe" in paths
    assert "/health/signals" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    monkeypatch.delenv("CACHE_REDIS_URL", raising=False)
    monkeypatch.delenv("STORAGE_ENDPOINT", raising=False)
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    return TestClient(app)


def test_probe_route_roundtrip(client) -> None:
    resp = client.post("/health/probe", json={"org_id": "org-1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["overall_status"] in ("HEALTHY", "DEGRADED", "UNKNOWN", "DOWN")
    assert len(body["signals"]) == 4
    # database probe against the test sqlite engine is healthy.
    by_name = {s["subsystem"]: s["status"] for s in body["signals"]}
    assert by_name["database"] == "HEALTHY"
    assert by_name["cache"] == "UNKNOWN"

    listed = client.get("/health/signals", params={"org_id": "org-1"})
    assert listed.status_code == 200
    assert len(listed.json()["signals"]) == 4


def test_operator_signal_route(client) -> None:
    created = client.post(
        "/health/signals",
        json={
            "org_id": "org-1",
            "signal_id": "sig-op",
            "subsystem": "publishing",
            "status": "DEGRADED",
            "detail": "mock adapter degraded",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "DEGRADED"

    conflict = client.post(
        "/health/signals",
        json={
            "org_id": "org-1",
            "signal_id": "sig-op",
            "subsystem": "publishing",
            "status": "HEALTHY",
        },
    )
    assert conflict.status_code == 409

    bad = client.post(
        "/health/signals",
        json={
            "org_id": "org-1",
            "signal_id": "bad/id",
            "subsystem": "publishing",
            "status": "HEALTHY",
        },
    )
    assert bad.status_code == 422
