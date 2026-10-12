"""Tests for the render project DSL and validation (CLIP-050)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.content_planning import ContentBrief, ContentPlanningStore
from clipos_api.main import app
from clipos_api.render_project import (
    RenderProject,
    RenderProjectStore,
    validate_render_spec,
)
from clipos_api.research import ResearchPacket, ResearchStore


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-050-render-projects-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> RenderProjectStore:
    return RenderProjectStore.from_engine(engine)


def _valid_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "version": 1,
        "inputs": {"candidate_id": "candidate-1"},
        "composition": {
            "aspect": "9:16",
            "duration_ms": 30000,
            "captions": {"enabled": True, "style_ref": "brand:cap-1"},
            "audio_normalization": {"enabled": True, "target_lufs": -14},
        },
        "outputs": [{"platform": "youtube"}, {"platform": "tiktok"}],
    }
    spec.update(overrides)
    return spec


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


def _project(
    render_project_id: str = "render-1", spec: dict[str, Any] | None = None, **overrides: object
) -> RenderProject:
    payload: dict[str, object] = {
        "render_project_id": render_project_id,
        "org_id": "org-1",
        "brief_id": "brief-1",
        "name": "Fixture render",
        "spec": spec if spec is not None else _valid_spec(),
    }
    payload.update(overrides)
    return RenderProject(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure DSL validator
# ---------------------------------------------------------------------------


def test_valid_spec_has_no_errors() -> None:
    assert validate_render_spec(_valid_spec()) == []


def test_minimal_valid_spec() -> None:
    spec = {
        "version": 1,
        "inputs": {"candidate_id": "c"},
        "composition": {"aspect": "16:9", "duration_ms": 1000},
        "outputs": [{"platform": "youtube"}],
    }
    assert validate_render_spec(spec) == []


def test_version_must_be_one() -> None:
    errors = validate_render_spec(_valid_spec(version=2))
    assert any("version must be 1" in error for error in errors)


def test_unknown_top_level_key_rejected() -> None:
    errors = validate_render_spec(_valid_spec(bogus={}))
    assert any("unknown top-level key: bogus" in error for error in errors)


def test_missing_inputs_rejected() -> None:
    spec = _valid_spec()
    del spec["inputs"]
    errors = validate_render_spec(spec)
    assert any("inputs must be an object" in error for error in errors)


def test_missing_candidate_id_rejected() -> None:
    errors = validate_render_spec(_valid_spec(inputs={}))
    assert any("candidate_id" in error for error in errors)


def test_bad_aspect_rejected() -> None:
    errors = validate_render_spec(_valid_spec(composition={"aspect": "2:1", "duration_ms": 5000}))
    assert any("composition.aspect" in error for error in errors)


def test_duration_bounds_enforced() -> None:
    too_short = _valid_spec(composition={"aspect": "9:16", "duration_ms": 500})
    assert any("duration_ms" in error for error in validate_render_spec(too_short))
    too_long = _valid_spec(composition={"aspect": "9:16", "duration_ms": 600_001})
    assert any("duration_ms" in error for error in validate_render_spec(too_long))


def test_empty_outputs_rejected() -> None:
    errors = validate_render_spec(_valid_spec(outputs=[]))
    assert any("outputs must be a non-empty array" in error for error in errors)


def test_duplicate_output_platform_rejected() -> None:
    errors = validate_render_spec(
        _valid_spec(outputs=[{"platform": "youtube"}, {"platform": "youtube"}])
    )
    assert any("duplicate output platform" in error for error in errors)


def test_captions_shape_validated() -> None:
    errors = validate_render_spec(
        _valid_spec(
            composition={"aspect": "9:16", "duration_ms": 5000, "captions": {"enabled": "yes"}}
        )
    )
    assert any("captions.enabled" in error for error in errors)


def test_audio_lufs_bounds_enforced() -> None:
    errors = validate_render_spec(
        _valid_spec(
            composition={
                "aspect": "9:16",
                "duration_ms": 5000,
                "audio_normalization": {"enabled": True, "target_lufs": -40},
            }
        )
    )
    assert any("target_lufs" in error for error in errors)


def test_audio_enabled_requires_target() -> None:
    errors = validate_render_spec(
        _valid_spec(
            composition={
                "aspect": "9:16",
                "duration_ms": 5000,
                "audio_normalization": {"enabled": True},
            }
        )
    )
    assert any("target_lufs is required" in error for error in errors)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_register_requires_existing_brief(store: RenderProjectStore) -> None:
    with pytest.raises(ValueError, match="content brief not found"):
        store.register_project(_project())


def test_register_rejects_invalid_spec(store: RenderProjectStore, engine: Engine) -> None:
    _seed_brief(engine)
    with pytest.raises(ValueError, match="invalid render spec"):
        store.register_project(_project(spec=_valid_spec(version=9)))


def test_register_idempotent(store: RenderProjectStore, engine: Engine) -> None:
    _seed_brief(engine)
    first = store.register_project(_project())
    second = store.register_project(_project())
    assert first.render_project_id == second.render_project_id
    assert first.spec["version"] == 1
    assert len(store.list_projects("org-1", "brief-1")) == 1


def test_register_conflict_on_different_fields(store: RenderProjectStore, engine: Engine) -> None:
    _seed_brief(engine)
    store.register_project(_project())
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_project(_project(name="Different name"))


def test_get_project_org_scoped(store: RenderProjectStore, engine: Engine) -> None:
    _seed_brief(engine)
    store.register_project(_project())
    assert store.get_project("render-1", "org-1") is not None
    assert store.get_project("render-1", "org-other") is None
    assert store.get_project("missing", "org-1") is None


def test_list_projects_missing_brief_raises(store: RenderProjectStore) -> None:
    with pytest.raises(ValueError, match="content brief not found"):
        store.list_projects("org-1", "missing")


def test_register_audit_recorded(
    store: RenderProjectStore, engine: Engine, audit_log: Path
) -> None:
    _seed_brief(engine)
    store.register_project(_project())
    log = audit_log.read_text(encoding="utf-8")
    assert "render_projects.registered" in log


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/render-specs/validate" in paths
    assert "/render-projects" in paths
    assert "/render-projects/{render_project_id}" in paths


def test_router_meta() -> None:
    from clipos_api.render_project import router

    assert router.tags == ["render-projects"]


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


def test_route_dry_run_validation(client) -> None:
    valid = client.post("/render-specs/validate", json={"spec": _valid_spec()})
    assert valid.status_code == 200
    assert valid.json() == {"valid": True, "errors": []}

    invalid = client.post("/render-specs/validate", json={"spec": _valid_spec(version=7)})
    assert invalid.status_code == 200
    body = invalid.json()
    assert body["valid"] is False
    assert any("version must be 1" in error for error in body["errors"])


def test_route_register_and_fetch_roundtrip(client) -> None:
    _seed_brief_http(client)
    response = client.post(
        "/render-projects",
        json={
            "render_project_id": "render-1",
            "org_id": "org-1",
            "brief_id": "brief-1",
            "name": "Fixture render",
            "spec": _valid_spec(),
        },
    )
    assert response.status_code == 200
    assert response.json()["spec"]["version"] == 1

    fetched = client.get("/render-projects/render-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Fixture render"

    listing = client.get("/render-projects", params={"org_id": "org-1", "brief_id": "brief-1"})
    assert len(listing.json()) == 1


def test_route_invalid_spec_maps_to_422(client) -> None:
    _seed_brief_http(client)
    response = client.post(
        "/render-projects",
        json={
            "render_project_id": "render-1",
            "org_id": "org-1",
            "brief_id": "brief-1",
            "name": "Fixture render",
            "spec": _valid_spec(outputs=[]),
        },
    )
    assert response.status_code == 422
    assert "invalid render spec" in response.json()["detail"]


def test_route_missing_brief_maps_to_404(client) -> None:
    response = client.post(
        "/render-projects",
        json={
            "render_project_id": "render-1",
            "org_id": "org-1",
            "brief_id": "missing",
            "name": "Fixture render",
            "spec": _valid_spec(),
        },
    )
    assert response.status_code == 404
    fetched = client.get("/render-projects/missing", params={"org_id": "org-1"})
    assert fetched.status_code == 404


def test_route_conflict_maps_to_409(client) -> None:
    _seed_brief_http(client)
    payload = {
        "render_project_id": "render-1",
        "org_id": "org-1",
        "brief_id": "brief-1",
        "name": "Fixture render",
        "spec": _valid_spec(),
    }
    assert client.post("/render-projects", json=payload).status_code == 200
    payload["name"] = "Renamed"
    assert client.post("/render-projects", json=payload).status_code == 409
