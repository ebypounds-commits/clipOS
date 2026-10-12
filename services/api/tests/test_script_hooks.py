"""Tests for hooks, scripts, and script versioning (CLIP-048)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.content_planning import ContentBrief, ContentPlanningStore
from clipos_api.main import app
from clipos_api.research import ResearchPacket, ResearchStore
from clipos_api.script_hooks import (
    HookCreate,
    Script,
    ScriptingStore,
    ScriptVersionRequest,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-048-script-hooks-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ScriptingStore:
    return ScriptingStore.from_engine(engine)


def _seed_brief(engine: Engine) -> None:
    ResearchStore.from_engine(engine).register_packet(
        ResearchPacket(research_packet_id="packet-1", org_id="org-1", title="Fixture")
    )
    ContentPlanningStore.from_engine(engine).register_brief(
        ContentBrief(
            brief_id="brief-1",
            org_id="org-1",
            research_packet_id="packet-1",
            title="Fixture brief",
            objective="Objective",
        )
    )


def _script(script_id: str = "script-1", **overrides: object) -> Script:
    payload: dict[str, object] = {
        "script_id": script_id,
        "org_id": "org-1",
        "brief_id": "brief-1",
        "title": "Fixture script",
    }
    payload.update(overrides)
    return Script(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def test_register_hook_requires_existing_brief(store: ScriptingStore) -> None:
    with pytest.raises(ValueError, match="content brief not found"):
        store.register_hook(HookCreate(org_id="org-1", brief_id="missing", text="A hook line"))


def test_register_hook_semantic_idempotent(store: ScriptingStore, engine: Engine) -> None:
    _seed_brief(engine)
    request = HookCreate(org_id="org-1", brief_id="brief-1", text="A hook line")
    first = store.register_hook(request)
    second = store.register_hook(request)
    assert first.hook_id == second.hook_id
    assert len(store.list_hooks("org-1", "brief-1")) == 1


def test_register_hook_distinct_texts_create_rows(store: ScriptingStore, engine: Engine) -> None:
    _seed_brief(engine)
    store.register_hook(HookCreate(org_id="org-1", brief_id="brief-1", text="Hook one"))
    store.register_hook(HookCreate(org_id="org-1", brief_id="brief-1", text="Hook two"))
    assert len(store.list_hooks("org-1", "brief-1")) == 2


def test_list_hooks_missing_brief_raises(store: ScriptingStore) -> None:
    with pytest.raises(ValueError, match="content brief not found"):
        store.list_hooks("org-1", "missing")


def test_hook_audit_recorded(store: ScriptingStore, engine: Engine, audit_log: Path) -> None:
    _seed_brief(engine)
    store.register_hook(HookCreate(org_id="org-1", brief_id="brief-1", text="Hook"))
    assert "hooks.registered" in audit_log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------


def test_register_script_requires_existing_brief(store: ScriptingStore) -> None:
    with pytest.raises(ValueError, match="content brief not found"):
        store.register_script(_script())


def test_register_script_idempotent(store: ScriptingStore, engine: Engine) -> None:
    _seed_brief(engine)
    first = store.register_script(_script())
    second = store.register_script(_script())
    assert first.script_id == second.script_id
    assert first.current_version == 0


def test_register_script_conflict_on_different_fields(
    store: ScriptingStore, engine: Engine
) -> None:
    _seed_brief(engine)
    store.register_script(_script())
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_script(_script(title="Different title"))


def test_get_script_org_scoped(store: ScriptingStore, engine: Engine) -> None:
    _seed_brief(engine)
    store.register_script(_script())
    assert store.get_script("script-1", "org-1") is not None
    assert store.get_script("script-1", "org-other") is None
    assert store.get_script("missing", "org-1") is None


def test_script_audit_recorded(store: ScriptingStore, engine: Engine, audit_log: Path) -> None:
    _seed_brief(engine)
    store.register_script(_script())
    assert "scripts.registered" in audit_log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Script versions
# ---------------------------------------------------------------------------


def test_save_version_requires_existing_script(store: ScriptingStore) -> None:
    with pytest.raises(ValueError, match="script not found"):
        store.save_version(
            "missing",
            ScriptVersionRequest(org_id="org-1", body="Body", author_ref="writer-1"),
        )


def test_save_first_version_bumps_current(
    store: ScriptingStore, engine: Engine, audit_log: Path
) -> None:
    _seed_brief(engine)
    store.register_script(_script())
    version = store.save_version(
        "script-1",
        ScriptVersionRequest(org_id="org-1", body="First body", author_ref="writer-1"),
    )
    assert version.version == 1
    assert version.body == "First body"
    script = store.get_script("script-1", "org-1")
    assert script is not None
    assert script.current_version == 1
    assert "scripts.version_appended" in audit_log.read_text(encoding="utf-8")


def test_save_identical_body_is_noop(store: ScriptingStore, engine: Engine) -> None:
    _seed_brief(engine)
    store.register_script(_script())
    first = store.save_version(
        "script-1",
        ScriptVersionRequest(org_id="org-1", body="Same body", author_ref="writer-1"),
    )
    second = store.save_version(
        "script-1",
        ScriptVersionRequest(org_id="org-1", body="Same body", author_ref="writer-2"),
    )
    assert first.version_id == second.version_id
    assert len(store.list_versions("script-1", "org-1")) == 1


def test_save_changed_body_appends_version(store: ScriptingStore, engine: Engine) -> None:
    _seed_brief(engine)
    store.register_script(_script())
    store.save_version(
        "script-1",
        ScriptVersionRequest(org_id="org-1", body="v1 body", author_ref="writer-1"),
    )
    second = store.save_version(
        "script-1",
        ScriptVersionRequest(org_id="org-1", body="v2 body", author_ref="writer-1"),
    )
    assert second.version == 2
    script = store.get_script("script-1", "org-1")
    assert script is not None
    assert script.current_version == 2
    versions = store.list_versions("script-1", "org-1")
    assert [version.version for version in versions] == [1, 2]


def test_list_versions_missing_script_raises(store: ScriptingStore) -> None:
    with pytest.raises(ValueError, match="script not found"):
        store.list_versions("missing", "org-1")


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/hooks" in paths
    assert "/scripts" in paths
    assert "/scripts/{script_id}" in paths
    assert "/scripts/{script_id}/versions" in paths


def test_router_meta() -> None:
    from clipos_api.script_hooks import router

    assert router.tags == ["scripting"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def _seed_brief_http(client) -> None:
    assert (
        client.post(
            "/research-packets",
            json={"research_packet_id": "packet-1", "org_id": "org-1", "title": "Fixture"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/content-briefs",
            json={
                "brief_id": "brief-1",
                "org_id": "org-1",
                "research_packet_id": "packet-1",
                "title": "Fixture brief",
                "objective": "Objective",
            },
        ).status_code
        == 200
    )


def test_route_hook_and_script_roundtrip(client) -> None:
    _seed_brief_http(client)

    hook = client.post(
        "/hooks",
        json={"org_id": "org-1", "brief_id": "brief-1", "text": "Stop scrolling"},
    )
    assert hook.status_code == 200
    assert hook.json()["hook_id"]
    hooks = client.get("/hooks", params={"org_id": "org-1", "brief_id": "brief-1"})
    assert len(hooks.json()) == 1

    script = client.post(
        "/scripts",
        json={
            "script_id": "script-1",
            "org_id": "org-1",
            "brief_id": "brief-1",
            "title": "Fixture script",
        },
    )
    assert script.status_code == 200
    assert script.json()["current_version"] == 0

    version = client.post(
        "/scripts/script-1/versions",
        json={"org_id": "org-1", "body": "Hello world", "author_ref": "writer-1"},
    )
    assert version.status_code == 200
    assert version.json()["version"] == 1

    # identical body over HTTP -> same version
    again = client.post(
        "/scripts/script-1/versions",
        json={"org_id": "org-1", "body": "Hello world", "author_ref": "writer-2"},
    )
    assert again.json()["version"] == 1

    versions = client.get("/scripts/script-1/versions", params={"org_id": "org-1"})
    assert len(versions.json()) == 1

    fetched = client.get("/scripts/script-1", params={"org_id": "org-1"})
    assert fetched.json()["current_version"] == 1


def test_route_missing_brief_maps_to_404(client) -> None:
    hook = client.post("/hooks", json={"org_id": "org-1", "brief_id": "missing", "text": "Hook"})
    assert hook.status_code == 404
    hooks = client.get("/hooks", params={"org_id": "org-1", "brief_id": "missing"})
    assert hooks.status_code == 404


def test_route_missing_script_maps_to_404(client) -> None:
    version = client.post(
        "/scripts/missing/versions",
        json={"org_id": "org-1", "body": "Body", "author_ref": "writer-1"},
    )
    assert version.status_code == 404
    fetched = client.get("/scripts/missing", params={"org_id": "org-1"})
    assert fetched.status_code == 404
    versions = client.get("/scripts/missing/versions", params={"org_id": "org-1"})
    assert versions.status_code == 404


def test_route_script_conflict_maps_to_409(client) -> None:
    _seed_brief_http(client)
    payload = {
        "script_id": "script-1",
        "org_id": "org-1",
        "brief_id": "brief-1",
        "title": "Fixture script",
    }
    assert client.post("/scripts", json=payload).status_code == 200
    payload["title"] = "Different"
    assert client.post("/scripts", json=payload).status_code == 409


def test_route_list_scripts_by_brief(client) -> None:
    _seed_brief_http(client)
    client.post(
        "/scripts",
        json={
            "script_id": "script-1",
            "org_id": "org-1",
            "brief_id": "brief-1",
            "title": "Fixture script",
        },
    )
    listing = client.get("/scripts", params={"org_id": "org-1", "brief_id": "brief-1"})
    assert listing.status_code == 200
    assert len(listing.json()) == 1
