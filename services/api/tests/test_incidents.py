"""Tests for incident lifecycle and runbooks (CLIP-087)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.incidents import (
    IncidentStatus,
    IncidentStore,
    IncidentTransitionRequest,
    OpenIncidentRequest,
    RecordTimelineRequest,
    RegisterRunbookRequest,
    RunbookStep,
    Severity,
    incident_transition_allowed,
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
    path = Path("/tmp") / "clipos-087-incidents-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> IncidentStore:
    return IncidentStore.from_engine(engine)


def _runbook(**overrides: object) -> RegisterRunbookRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "runbook_id": "rb-1",
        "title": "DB outage",
        "category": "database",
        "steps": [
            RunbookStep(step_no=1, instruction="Check connections", command_ref="cmd://pg-stat"),
            RunbookStep(step_no=2, instruction="Failover to replica"),
        ],
    }
    payload.update(overrides)
    return RegisterRunbookRequest(**payload)  # type: ignore[arg-type]


def _incident(**overrides: object) -> OpenIncidentRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "incident_id": "inc-1",
        "title": "DB latency spike",
        "subsystem": "database",
        "category": "database",
        "severity": Severity.SEV2,
        "actor_ref": "op-1",
    }
    payload.update(overrides)
    return OpenIncidentRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure lifecycle
# ---------------------------------------------------------------------------


def test_lifecycle_paths() -> None:
    assert incident_transition_allowed(IncidentStatus.OPEN, IncidentStatus.ACKNOWLEDGED)
    assert incident_transition_allowed(IncidentStatus.ACKNOWLEDGED, IncidentStatus.MITIGATED)
    assert incident_transition_allowed(IncidentStatus.MITIGATED, IncidentStatus.RESOLVED)
    assert incident_transition_allowed(IncidentStatus.RESOLVED, IncidentStatus.CLOSED)
    assert incident_transition_allowed(IncidentStatus.ACKNOWLEDGED, IncidentStatus.CLOSED)
    assert incident_transition_allowed(IncidentStatus.MITIGATED, IncidentStatus.OPEN)


def test_lifecycle_illegal_and_terminal() -> None:
    assert not incident_transition_allowed(IncidentStatus.OPEN, IncidentStatus.RESOLVED)
    assert not incident_transition_allowed(IncidentStatus.CLOSED, IncidentStatus.OPEN)
    assert not incident_transition_allowed(IncidentStatus.RESOLVED, IncidentStatus.MITIGATED)
    assert incident_transition_allowed(IncidentStatus.CLOSED, IncidentStatus.CLOSED)


# ---------------------------------------------------------------------------
# Runbooks
# ---------------------------------------------------------------------------


def test_runbook_register_idempotent_conflict_audit(
    store: IncidentStore,
    audit_log: Path,
) -> None:
    first = store.register_runbook(_runbook())
    assert [s.step_no for s in first.steps] == [1, 2]
    again = store.register_runbook(_runbook())
    assert again.runbook_id == first.runbook_id
    assert "runbooks.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_runbook(_runbook(title="Changed"))
    assert len(store.list_runbooks("org-1")) == 1


def test_runbook_get_with_steps(store: IncidentStore) -> None:
    store.register_runbook(_runbook())
    fetched = store.get_runbook("rb-1", "org-1")
    assert fetched is not None
    assert len(fetched.steps) == 2
    assert fetched.steps[0].command_ref == "cmd://pg-stat"
    assert store.get_runbook("rb-1", "org-other") is None


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


def test_open_incident_with_matching_runbook(
    store: IncidentStore,
    audit_log: Path,
) -> None:
    store.register_runbook(_runbook())
    incident = store.open_incident(_incident(runbook_id="rb-1"))
    assert incident.status is IncidentStatus.OPEN
    assert incident.severity is Severity.SEV2
    assert incident.runbook_id == "rb-1"
    log = audit_log.read_text(encoding="utf-8")
    assert "incidents.opened" in log
    # Opening also seeds the timeline.
    timeline = store.list_timeline("inc-1", "org-1")
    assert len(timeline) == 1
    assert timeline[0].kind == "opened"


def test_open_incident_runbook_category_mismatch(store: IncidentStore) -> None:
    store.register_runbook(_runbook(category="network"))
    with pytest.raises(ValueError, match="category mismatch"):
        store.open_incident(_incident(runbook_id="rb-1"))
    with pytest.raises(ValueError, match="runbook not found"):
        store.open_incident(_incident(runbook_id="missing"))


def test_open_incident_idempotent_conflict(store: IncidentStore) -> None:
    store.open_incident(_incident())
    again = store.open_incident(_incident())
    assert again.incident_id == "inc-1"
    with pytest.raises(ValueError, match="already opened with different fields"):
        store.open_incident(_incident(title="Changed"))


def test_incident_full_lifecycle(
    store: IncidentStore,
    audit_log: Path,
) -> None:
    store.open_incident(_incident())
    for status in (
        IncidentStatus.ACKNOWLEDGED,
        IncidentStatus.MITIGATED,
        IncidentStatus.RESOLVED,
        IncidentStatus.CLOSED,
    ):
        store.transition_incident(
            "inc-1",
            "org-1",
            IncidentTransitionRequest(status=status, actor_ref="op"),
        )
    assert store.get_incident("inc-1", "org-1").status is IncidentStatus.CLOSED
    assert "incidents.status_changed" in audit_log.read_text(encoding="utf-8")
    # Timeline captured opened + 4 moves.
    assert len(store.list_timeline("inc-1", "org-1")) == 5
    with pytest.raises(ValueError, match="illegal incident transition"):
        store.transition_incident(
            "inc-1",
            "org-1",
            IncidentTransitionRequest(status=IncidentStatus.OPEN, actor_ref="op"),
        )


def test_incident_regression_reopen(store: IncidentStore) -> None:
    store.open_incident(_incident())
    store.transition_incident(
        "inc-1",
        "org-1",
        IncidentTransitionRequest(status=IncidentStatus.ACKNOWLEDGED, actor_ref="op"),
    )
    store.transition_incident(
        "inc-1", "org-1", IncidentTransitionRequest(status=IncidentStatus.MITIGATED, actor_ref="op")
    )
    reopened = store.transition_incident(
        "inc-1", "org-1", IncidentTransitionRequest(status=IncidentStatus.OPEN, actor_ref="op")
    )
    assert reopened.status is IncidentStatus.OPEN


def test_incident_list_status_filter(store: IncidentStore) -> None:
    store.open_incident(_incident())
    store.open_incident(_incident(incident_id="inc-2"))
    store.transition_incident(
        "inc-2",
        "org-1",
        IncidentTransitionRequest(status=IncidentStatus.ACKNOWLEDGED, actor_ref="op"),
    )
    assert len(store.list_incidents("org-1")) == 2
    assert len(store.list_incidents("org-1", IncidentStatus.OPEN)) == 1
    assert len(store.list_incidents("org-1", IncidentStatus.ACKNOWLEDGED)) == 1
    assert len(store.list_incidents("org-other")) == 0


def test_incident_unknown_404(store: IncidentStore) -> None:
    with pytest.raises(ValueError, match="incident not found"):
        store.transition_incident(
            "missing",
            "org-1",
            IncidentTransitionRequest(status=IncidentStatus.ACKNOWLEDGED, actor_ref="op"),
        )


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


def test_timeline_append_idempotent_conflict(
    store: IncidentStore,
    audit_log: Path,
) -> None:
    store.open_incident(_incident())
    entry = store.record_timeline(
        "inc-1",
        "org-1",
        RecordTimelineRequest(
            entry_id="note-1", kind="note", note_ref="doc://postmortem", actor_ref="op"
        ),
    )
    assert entry.kind == "note"
    again = store.record_timeline(
        "inc-1",
        "org-1",
        RecordTimelineRequest(
            entry_id="note-1", kind="note", note_ref="doc://postmortem", actor_ref="op"
        ),
    )
    assert again.entry_id == entry.entry_id
    assert "incident_timeline.recorded" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.record_timeline(
            "inc-1",
            "org-1",
            RecordTimelineRequest(entry_id="note-1", kind="note", actor_ref="other"),
        )
    with pytest.raises(ValueError, match="incident not found"):
        store.record_timeline(
            "missing",
            "org-1",
            RecordTimelineRequest(entry_id="note-2", kind="note", actor_ref="op"),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/runbooks" in paths
    assert "/incidents" in paths
    assert "/incidents/{incident_id}/transition" in paths
    assert "/incidents/{incident_id}/timeline" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _runbook_payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "runbook_id": "rb-1",
        "title": "DB outage",
        "category": "database",
        "steps": [{"step_no": 1, "instruction": "Check connections"}],
    }


def _incident_payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "incident_id": "inc-1",
        "title": "DB latency spike",
        "subsystem": "database",
        "category": "database",
        "severity": "SEV2",
        "runbook_id": "rb-1",
        "actor_ref": "op-1",
    }


def test_full_route_roundtrip(client) -> None:
    runbook = client.post("/runbooks", json=_runbook_payload())
    assert runbook.status_code == 200, runbook.text
    assert len(runbook.json()["steps"]) == 1

    fetched = client.get("/runbooks/rb-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert client.get("/runbooks/nope", params={"org_id": "org-1"}).status_code == 404

    opened = client.post("/incidents", json=_incident_payload())
    assert opened.status_code == 200, opened.text
    assert opened.json()["status"] == "OPEN"

    assert client.get("/incidents/inc-1", params={"org_id": "org-1"}).status_code == 200

    for status in ("ACKNOWLEDGED", "MITIGATED", "RESOLVED", "CLOSED"):
        resp = client.post(
            "/incidents/inc-1/transition",
            params={"org_id": "org-1"},
            json={"status": status, "actor_ref": "op"},
        )
        assert resp.status_code == 200, (status, resp.text)

    timeline = client.get("/incidents/inc-1/timeline", params={"org_id": "org-1"})
    assert timeline.status_code == 200
    assert len(timeline.json()) == 5

    listing = client.get("/incidents", params={"org_id": "org-1", "status": "CLOSED"})
    assert len(listing.json()) == 1


def test_route_errors(client) -> None:
    client.post("/runbooks", json=_runbook_payload())
    mismatch = client.post("/incidents", json={**_incident_payload(), "category": "network"})
    assert mismatch.status_code == 409

    missing_runbook = client.post(
        "/incidents", json={**_incident_payload(), "runbook_id": "missing"}
    )
    assert missing_runbook.status_code == 404

    client.post("/incidents", json={**_incident_payload(), "runbook_id": None})
    illegal = client.post(
        "/incidents/inc-1/transition",
        params={"org_id": "org-1"},
        json={"status": "RESOLVED", "actor_ref": "op"},
    )
    assert illegal.status_code == 409

    bad_slug = client.post("/runbooks", json={**_runbook_payload(), "runbook_id": "bad/id"})
    assert bad_slug.status_code == 422

    no_steps = client.post(
        "/runbooks",
        json={**_runbook_payload(), "runbook_id": "rb-empty", "steps": []},
    )
    assert no_steps.status_code == 422
