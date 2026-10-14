"""Tests for channel incubation/launch experiments (CLIP-079)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.brand_templates import (
    BrandTemplateStore,
    RegisterBrandTemplateRequest,
    RegisterChannelTemplateRequest,
)
from clipos_api.incubation import (
    IncubationStatus,
    IncubationStore,
    RegisterRunRequest,
    RunTransitionRequest,
    run_transition_allowed,
)
from clipos_api.main import app
from clipos_api.publishing import Visibility


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-079-incubation-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> IncubationStore:
    return IncubationStore.from_engine(engine)


def _run(**overrides: object) -> RegisterRunRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "run_id": "inc-1",
        "platform": "tiktok",
        "channel_ref": "channel://new-series",
        "goal_ref": "goal://1k-followers",
    }
    payload.update(overrides)
    return RegisterRunRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure lifecycle
# ---------------------------------------------------------------------------


def test_incubation_happy_paths() -> None:
    assert run_transition_allowed(IncubationStatus.DRAFT, IncubationStatus.INCUBATING)
    assert run_transition_allowed(IncubationStatus.INCUBATING, IncubationStatus.LAUNCHED)
    assert run_transition_allowed(IncubationStatus.INCUBATING, IncubationStatus.SUNSET)
    assert run_transition_allowed(IncubationStatus.LAUNCHED, IncubationStatus.ARCHIVED)
    assert run_transition_allowed(IncubationStatus.SUNSET, IncubationStatus.ARCHIVED)


def test_cannot_skip_incubation() -> None:
    assert not run_transition_allowed(IncubationStatus.DRAFT, IncubationStatus.LAUNCHED)
    assert not run_transition_allowed(IncubationStatus.DRAFT, IncubationStatus.ARCHIVED)
    assert not run_transition_allowed(IncubationStatus.LAUNCHED, IncubationStatus.INCUBATING)
    assert not run_transition_allowed(IncubationStatus.ARCHIVED, IncubationStatus.INCUBATING)


def test_same_state_noop() -> None:
    assert run_transition_allowed(IncubationStatus.INCUBATING, IncubationStatus.INCUBATING)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_register_idempotent_conflict_audit(
    store: IncubationStore,
    audit_log: Path,
) -> None:
    first = store.register_run(_run())
    again = store.register_run(_run())
    assert first.run_id == again.run_id
    assert first.status is IncubationStatus.DRAFT
    assert "incubation_runs.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_run(_run(channel_ref="channel://other"))
    assert len(store.list_runs("org-1")) == 1


def test_register_validates_template_links(store: IncubationStore) -> None:
    with pytest.raises(ValueError, match="brand template not found"):
        store.register_run(_run(brand_template_id="missing"))
    with pytest.raises(ValueError, match="channel template not found"):
        store.register_run(_run(channel_template_id="missing"))


def test_register_with_templates(
    store: IncubationStore,
    engine: Engine,
) -> None:
    templates = BrandTemplateStore.from_engine(engine)
    templates.register_brand(
        RegisterBrandTemplateRequest(org_id="org-1", template_id="brand-1", title="Main")
    )
    templates.register_channel(
        RegisterChannelTemplateRequest(
            org_id="org-1",
            template_id="chan-1",
            platform="tiktok",
            title="TT",
            default_visibility=Visibility.PRIVATE,
        )
    )
    run = store.register_run(_run(brand_template_id="brand-1", channel_template_id="chan-1"))
    assert run.brand_template_id == "brand-1"
    assert run.channel_template_id == "chan-1"


def test_full_lifecycle_flow(store: IncubationStore, audit_log: Path) -> None:
    store.register_run(_run())
    started = store.transition_run(
        "inc-1",
        "org-1",
        RunTransitionRequest(status=IncubationStatus.INCUBATING, actor_ref="op"),
    )
    assert started.status is IncubationStatus.INCUBATING
    launched = store.transition_run(
        "inc-1",
        "org-1",
        RunTransitionRequest(
            status=IncubationStatus.LAUNCHED, actor_ref="op", reason_ref="goals-hit"
        ),
    )
    assert launched.status is IncubationStatus.LAUNCHED
    archived = store.transition_run(
        "inc-1",
        "org-1",
        RunTransitionRequest(status=IncubationStatus.ARCHIVED, actor_ref="op"),
    )
    assert archived.status is IncubationStatus.ARCHIVED
    assert audit_log.read_text(encoding="utf-8").count("incubation_runs.status_changed") == 3


def test_illegal_transitions(store: IncubationStore) -> None:
    store.register_run(_run())
    with pytest.raises(ValueError, match="illegal incubation transition"):
        store.transition_run(
            "inc-1",
            "org-1",
            RunTransitionRequest(status=IncubationStatus.LAUNCHED, actor_ref="op"),
        )
    store.transition_run(
        "inc-1", "org-1", RunTransitionRequest(status=IncubationStatus.INCUBATING, actor_ref="op")
    )
    store.transition_run(
        "inc-1", "org-1", RunTransitionRequest(status=IncubationStatus.SUNSET, actor_ref="op")
    )
    with pytest.raises(ValueError, match="illegal incubation transition"):
        store.transition_run(
            "inc-1", "org-1", RunTransitionRequest(status=IncubationStatus.LAUNCHED, actor_ref="op")
        )


def test_unknown_run_404(store: IncubationStore) -> None:
    with pytest.raises(ValueError, match="incubation run not found"):
        store.transition_run(
            "missing",
            "org-1",
            RunTransitionRequest(status=IncubationStatus.INCUBATING, actor_ref="op"),
        )
    assert store.get_run("missing", "org-1") is None


def test_list_status_filter(store: IncubationStore) -> None:
    store.register_run(_run())
    store.register_run(_run(run_id="inc-2"))
    store.transition_run(
        "inc-2", "org-1", RunTransitionRequest(status=IncubationStatus.INCUBATING, actor_ref="op")
    )
    assert len(store.list_runs("org-1")) == 2
    assert len(store.list_runs("org-1", IncubationStatus.DRAFT)) == 1
    assert len(store.list_runs("org-1", IncubationStatus.INCUBATING)) == 1
    assert len(store.list_runs("org-other")) == 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/incubation-runs" in paths
    assert "/incubation-runs/{run_id}" in paths
    assert "/incubation-runs/{run_id}/transition" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_route_lifecycle_roundtrip(client) -> None:
    created = client.post(
        "/incubation-runs",
        json={
            "org_id": "org-1",
            "run_id": "inc-1",
            "platform": "tiktok",
            "channel_ref": "channel://new-series",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "DRAFT"

    fetched = client.get("/incubation-runs/inc-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert client.get("/incubation-runs/nope", params={"org_id": "org-1"}).status_code == 404

    conflict = client.post(
        "/incubation-runs",
        json={
            "org_id": "org-1",
            "run_id": "inc-1",
            "platform": "tiktok",
            "channel_ref": "channel://changed",
        },
    )
    assert conflict.status_code == 409

    # start -> launch -> archive via routes
    for status in ("INCUBATING", "LAUNCHED", "ARCHIVED"):
        resp = client.post(
            "/incubation-runs/inc-1/transition",
            params={"org_id": "org-1"},
            json={"status": status, "actor_ref": "op"},
        )
        assert resp.status_code == 200, (status, resp.text)
        assert resp.json()["status"] == status

    listing = client.get("/incubation-runs", params={"org_id": "org-1", "status": "ARCHIVED"})
    assert len(listing.json()) == 1


def test_route_skip_incubation_409(client) -> None:
    client.post(
        "/incubation-runs",
        json={
            "org_id": "org-1",
            "run_id": "inc-1",
            "platform": "tiktok",
            "channel_ref": "channel://x",
        },
    )
    resp = client.post(
        "/incubation-runs/inc-1/transition",
        params={"org_id": "org-1"},
        json={"status": "LAUNCHED", "actor_ref": "op"},
    )
    assert resp.status_code == 409


def test_route_bad_slug_422(client) -> None:
    resp = client.post(
        "/incubation-runs",
        json={
            "org_id": "org-1",
            "run_id": "bad/id",
            "platform": "tiktok",
            "channel_ref": "channel://x",
        },
    )
    assert resp.status_code == 422
