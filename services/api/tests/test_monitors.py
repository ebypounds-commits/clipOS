"""Tests for credential/quota/disk/provider health monitors (CLIP-089)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.monitors import (
    MonitorCheck,
    MonitorKind,
    MonitorStatus,
    MonitorStore,
    RecordCheckRequest,
    check_credential_expiry,
    check_disk_usage,
    check_provider_health,
    check_quota_headroom,
    evaluate_monitor,
    summarize_monitors,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-089-monitors-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> MonitorStore:
    return MonitorStore.from_engine(engine)


# ---------------------------------------------------------------------------
# Pure monitors
# ---------------------------------------------------------------------------


def test_credential_expiry_bands() -> None:
    assert check_credential_expiry(60.0).status is MonitorStatus.HEALTHY
    assert check_credential_expiry(10.0).status is MonitorStatus.DEGRADED
    assert check_credential_expiry(-1.0).status is MonitorStatus.DOWN
    # Boundary: exactly at the warn window is DEGRADED.
    assert check_credential_expiry(14.0, warn_days=14.0).status is MonitorStatus.DEGRADED


def test_credential_expiry_nonpositive_warn_falls_back() -> None:
    result = check_credential_expiry(30.0, warn_days=0.0)
    assert result.status is MonitorStatus.HEALTHY
    assert result.threshold == 14.0


def test_quota_headroom_bands() -> None:
    assert check_quota_headroom(10.0, 100.0).status is MonitorStatus.HEALTHY
    assert check_quota_headroom(85.0, 100.0).status is MonitorStatus.DEGRADED
    assert check_quota_headroom(100.0, 100.0).status is MonitorStatus.DOWN
    assert check_quota_headroom(150.0, 100.0).status is MonitorStatus.DOWN


def test_quota_headroom_invalid_limit() -> None:
    zero = check_quota_headroom(5.0, 0.0)
    assert zero.status is MonitorStatus.DOWN
    assert zero.detail == "limit_invalid"
    negative = check_quota_headroom(5.0, -1.0)
    assert negative.status is MonitorStatus.DOWN


def test_quota_headroom_custom_warn_ratio() -> None:
    assert check_quota_headroom(50.0, 100.0, 0.5).status is MonitorStatus.DEGRADED
    assert check_quota_headroom(49.0, 100.0, 0.5).status is MonitorStatus.HEALTHY


def test_disk_usage_bands() -> None:
    gb = 1024**3
    assert check_disk_usage(10 * gb, 100 * gb).status is MonitorStatus.HEALTHY
    assert check_disk_usage(90 * gb, 100 * gb).status is MonitorStatus.DEGRADED
    full = check_disk_usage(100 * gb, 100 * gb)
    assert full.status is MonitorStatus.DOWN
    assert full.detail == "disk full"


def test_disk_usage_unknown_capacity() -> None:
    result = check_disk_usage(5.0, 0.0)
    assert result.status is MonitorStatus.DOWN
    assert result.detail == "capacity_unknown"


def test_provider_health_mapping() -> None:
    ok = check_provider_health(True, "200 ok")
    assert ok.status is MonitorStatus.HEALTHY
    down = check_provider_health(False, "connection refused")
    assert down.status is MonitorStatus.DOWN
    assert down.detail == "connection refused"
    truncated = check_provider_health(False, "x" * 500)
    assert len(truncated.detail) <= 255
    blank = check_provider_health(True, "   ")
    assert blank.detail == "provider reachable"


def test_evaluate_monitor_dispatch_and_missing_fields() -> None:
    good = evaluate_monitor(
        RecordCheckRequest(
            org_id="org-1",
            check_id="c1",
            subsystem="vault",
            monitor_kind=MonitorKind.CREDENTIAL_EXPIRY,
            subject_ref="vault://yt",
            days_left=3.0,
        )
    )
    assert good.status is MonitorStatus.DEGRADED
    with pytest.raises(ValueError, match="days_left required"):
        evaluate_monitor(
            RecordCheckRequest(
                org_id="org-1",
                check_id="c2",
                subsystem="vault",
                monitor_kind=MonitorKind.CREDENTIAL_EXPIRY,
                subject_ref="vault://yt",
            )
        )
    with pytest.raises(ValueError, match="used and limit required"):
        evaluate_monitor(
            RecordCheckRequest(
                org_id="org-1",
                check_id="c3",
                subsystem="quota",
                monitor_kind=MonitorKind.QUOTA_HEADROOM,
                subject_ref="quota://yt",
            )
        )
    with pytest.raises(ValueError, match="provider_ok required"):
        evaluate_monitor(
            RecordCheckRequest(
                org_id="org-1",
                check_id="c4",
                subsystem="provider",
                monitor_kind=MonitorKind.PROVIDER_HEALTH,
                subject_ref="provider://yt",
            )
        )


# ---------------------------------------------------------------------------
# Aggregation (pure)
# ---------------------------------------------------------------------------


def _check(kind: MonitorKind, status: MonitorStatus, check_id: str) -> MonitorCheck:
    return MonitorCheck(
        check_id=check_id,
        org_id="org-1",
        subsystem="sub",
        monitor_kind=kind,
        subject_ref="ref://x",
        status=status,
    )


def test_summarize_empty_is_unknown() -> None:
    summary = summarize_monitors("org-1", [])
    assert summary.overall_status is MonitorStatus.UNKNOWN
    assert summary.monitors == []
    assert summary.attention == []


def test_summarize_attention_and_worst_rank() -> None:
    checks = [
        _check(MonitorKind.CREDENTIAL_EXPIRY, MonitorStatus.HEALTHY, "a"),
        _check(MonitorKind.QUOTA_HEADROOM, MonitorStatus.DEGRADED, "b"),
        _check(MonitorKind.DISK_USAGE, MonitorStatus.DOWN, "c"),
        _check(MonitorKind.PROVIDER_HEALTH, MonitorStatus.HEALTHY, "d"),
    ]
    summary = summarize_monitors("org-1", checks)
    assert summary.overall_status is MonitorStatus.DOWN
    assert len(summary.monitors) == 4
    assert len(summary.attention) == 2
    assert {a.monitor_kind for a in summary.attention} == {
        MonitorKind.QUOTA_HEADROOM,
        MonitorKind.DISK_USAGE,
    }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_record_check_idempotent_conflict_audit(
    store: MonitorStore,
    audit_log: Path,
) -> None:
    request = RecordCheckRequest(
        org_id="org-1",
        check_id="chk-1",
        subsystem="quota",
        monitor_kind=MonitorKind.QUOTA_HEADROOM,
        subject_ref="quota://yt",
        used=85.0,
        limit=100.0,
    )
    first = store.record_check(request)
    assert first.status is MonitorStatus.DEGRADED
    assert first.measured == 85.0
    again = store.record_check(request)
    assert again.check_id == first.check_id
    assert "monitor_checks.recorded" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.record_check(
            RecordCheckRequest(
                org_id="org-1",
                check_id="chk-1",
                subsystem="quota",
                monitor_kind=MonitorKind.QUOTA_HEADROOM,
                subject_ref="quota://yt",
                used=10.0,
                limit=100.0,
            )
        )
    assert len(store.list_checks("org-1")) == 1


def test_record_check_provider_and_summarize(
    store: MonitorStore,
) -> None:
    store.record_check(
        RecordCheckRequest(
            org_id="org-1",
            check_id="chk-prov",
            subsystem="publishing",
            monitor_kind=MonitorKind.PROVIDER_HEALTH,
            subject_ref="provider://tiktok",
            provider_ok=False,
            provider_detail="timeout",
        )
    )
    store.record_check(
        RecordCheckRequest(
            org_id="org-1",
            check_id="chk-disk",
            subsystem="storage",
            monitor_kind=MonitorKind.DISK_USAGE,
            subject_ref="disk://data",
            used=10.0,
            limit=1000.0,
        )
    )
    summary = store.summarize("org-1")
    assert summary.overall_status is MonitorStatus.DOWN
    assert len(summary.attention) == 1
    assert summary.attention[0].monitor_kind is MonitorKind.PROVIDER_HEALTH

    provider_only = store.list_checks("org-1", MonitorKind.PROVIDER_HEALTH)
    assert len(provider_only) == 1
    assert len(store.list_checks("org-other")) == 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/monitors/check" in paths
    assert "/monitors" in paths
    assert "/monitors/checks" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_monitor_route_roundtrip(client) -> None:
    created = client.post(
        "/monitors/check",
        json={
            "org_id": "org-1",
            "check_id": "chk-1",
            "subsystem": "vault",
            "monitor_kind": "CREDENTIAL_EXPIRY",
            "subject_ref": "vault://yt-oauth",
            "days_left": 2.0,
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "DEGRADED"

    summary = client.get("/monitors", params={"org_id": "org-1"})
    assert summary.status_code == 200
    body = summary.json()
    assert body["overall_status"] == "DEGRADED"
    assert len(body["attention"]) == 1

    history = client.get(
        "/monitors/checks",
        params={"org_id": "org-1", "monitor_kind": "CREDENTIAL_EXPIRY"},
    )
    assert len(history.json()) == 1


def test_monitor_route_errors(client) -> None:
    missing_field = client.post(
        "/monitors/check",
        json={
            "org_id": "org-1",
            "check_id": "chk-bad",
            "subsystem": "quota",
            "monitor_kind": "QUOTA_HEADROOM",
            "subject_ref": "quota://yt",
        },
    )
    assert missing_field.status_code == 422

    ok_payload = {
        "org_id": "org-1",
        "check_id": "chk-ok",
        "subsystem": "disk",
        "monitor_kind": "DISK_USAGE",
        "subject_ref": "disk://data",
        "used": 10.0,
        "limit": 100.0,
    }
    assert client.post("/monitors/check", json=ok_payload).status_code == 200
    conflict = client.post("/monitors/check", json={**ok_payload, "used": 99.0})
    assert conflict.status_code == 409

    bad_slug = client.post("/monitors/check", json={**ok_payload, "check_id": "bad/id"})
    assert bad_slug.status_code == 422

    bad_ratio = client.post("/monitors/check", json={**ok_payload, "warn_ratio": 1.5})
    assert bad_ratio.status_code == 422
