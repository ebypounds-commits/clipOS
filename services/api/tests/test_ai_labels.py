"""Tests for generated-media provenance and AI-label metadata (CLIP-057)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.ai_labels import (
    GeneratedMediaMetadataRequest,
    GeneratedMediaStore,
    LabelValue,
    deterministic_label_id,
    record_generated_media,
)
from clipos_api.content_evals import ContentEvalStore
from clipos_api.main import app
from clipos_api.media_ingestion import MediaAssetKind


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-057-ai-labels-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def workdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "render-work"
    root.mkdir()
    monkeypatch.setenv("RENDER_WORKDIR", str(root))
    return root


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> GeneratedMediaStore:
    return GeneratedMediaStore.from_engine(engine)


@pytest.fixture()
def transformations(engine: Engine) -> ContentEvalStore:
    return ContentEvalStore.from_engine(engine)


def _seed_brief(engine: Engine) -> None:
    from clipos_api.content_planning import ContentBrief, ContentPlanningStore
    from clipos_api.research import ResearchPacket, ResearchStore

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


def _request(**overrides: object) -> GeneratedMediaMetadataRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "asset_kind": MediaAssetKind.IMAGE,
        "asset_ref": "gen/img.png",
        "generator_alias": "image-neutral-v1",
        "prompt": "A product shot on marble",
    }
    payload.update(overrides)
    return GeneratedMediaMetadataRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure id helpers
# ---------------------------------------------------------------------------


def test_label_id_deterministic_and_org_scoped() -> None:
    first = deterministic_label_id("org-1", "IMAGE", "gen/img.png", "image-neutral-v1")
    second = deterministic_label_id("org-1", "IMAGE", "gen/img.png", "image-neutral-v1")
    assert first == second
    assert first.startswith("org-1:ai-label:")
    assert len(first) <= 128
    assert first != deterministic_label_id("org-2", "IMAGE", "gen/img.png", "image-neutral-v1")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_register_label_idempotent(store: GeneratedMediaStore) -> None:
    request = _request()
    first = record_generated_media(store, request)
    second = record_generated_media(store, request)
    assert first.label_id == second.label_id
    assert first.label is LabelValue.AI_GENERATED
    assert first.generator_alias == "image-neutral-v1"
    assert len(store.list_labels("org-1")) == 1


def test_register_label_conflict_on_changed_fields(
    store: GeneratedMediaStore,
) -> None:
    record_generated_media(store, _request())
    # same deterministic id (kind+asset+alias unchanged), changed label
    with pytest.raises(ValueError, match="already registered with different fields"):
        record_generated_media(store, _request(label=LabelValue.AI_ASSISTED))


def test_list_labels_org_and_kind_scoped(store: GeneratedMediaStore) -> None:
    record_generated_media(store, _request())
    record_generated_media(
        store,
        _request(
            asset_kind=MediaAssetKind.AUDIO,
            asset_ref="gen/voice.wav",
            generator_alias="tts-neutral-v1",
        ),
    )
    assert len(store.list_labels("org-1")) == 2
    assert len(store.list_labels("org-1", MediaAssetKind.AUDIO)) == 1
    assert store.list_labels("org-other") == []


def test_get_label_org_scoped(store: GeneratedMediaStore) -> None:
    stored = record_generated_media(store, _request())
    assert store.get_label(stored.label_id, "org-1") is not None
    assert store.get_label(stored.label_id, "org-other") is None
    assert store.get_label("missing", "org-1") is None


def test_label_audit_recorded(store: GeneratedMediaStore, audit_log: Path) -> None:
    record_generated_media(store, _request())
    assert "ai_labels.recorded" in audit_log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Transformation provenance (CLIP-049 linkage)
# ---------------------------------------------------------------------------


def test_record_with_brief_appends_transformation(
    store: GeneratedMediaStore,
    transformations: ContentEvalStore,
    engine: Engine,
) -> None:
    _seed_brief(engine)
    record_generated_media(
        store, _request(brief_id="brief-1"), transformation_store=transformations
    )
    rows = transformations.list_transformations("org-1", "brief-1")
    assert len(rows) == 1
    assert rows[0].method == "image-generation"
    assert rows[0].from_ref.startswith("gen-prompt:")
    assert rows[0].to_ref == "gen/img.png"
    assert rows[0].description is not None


def test_record_without_brief_skips_transformation(
    store: GeneratedMediaStore,
    transformations: ContentEvalStore,
    engine: Engine,
) -> None:
    _seed_brief(engine)
    record_generated_media(store, _request(), transformation_store=transformations)
    # no brief -> no transformation row (voice synthesis path)
    assert transformations.list_transformations("org-1", "brief-1") == []


def test_record_missing_brief_maps_to_not_found_via_store(
    store: GeneratedMediaStore,
    transformations: ContentEvalStore,
    engine: Engine,
) -> None:
    with pytest.raises(ValueError, match="content brief not found"):
        record_generated_media(
            store, _request(brief_id="missing"), transformation_store=transformations
        )


# ---------------------------------------------------------------------------
# Route: auto-label integration with generation routes (PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/generated-media-metadata" in paths
    assert "/ai-labels" in paths
    assert "/ai-labels/{label_id}" in paths


def test_router_meta() -> None:
    from clipos_api.ai_labels import router

    assert router.tags == ["ai-labels"]


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


def test_route_manual_metadata_with_brief_creates_label_and_transformation(
    client, engine: Engine
) -> None:
    _seed_brief_http(client)
    response = client.post(
        "/generated-media-metadata",
        json={
            "org_id": "org-1",
            "brief_id": "brief-1",
            "asset_kind": "IMAGE",
            "asset_ref": "gen/img.png",
            "generator_alias": "image-neutral-v1",
            "prompt": "A product shot on marble",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["label"] == "AI_GENERATED"
    assert body["asset_kind"] == "IMAGE"

    listing = client.get("/ai-labels", params={"org_id": "org-1"})
    assert len(listing.json()) == 1
    fetched = client.get(f"/ai-labels/{body['label_id']}", params={"org_id": "org-1"})
    assert fetched.status_code == 200

    transformations = client.get(
        "/transformation-records", params={"org_id": "org-1", "brief_id": "brief-1"}
    )
    assert transformations.status_code == 200
    assert len(transformations.json()) == 1


def test_route_image_generation_auto_labels(client, engine: Engine, workdir: Path) -> None:
    _seed_brief_http(client)
    generated = client.post(
        "/image-generations",
        json={
            "image_asset_id": "img-1",
            "org_id": "org-1",
            "brief_id": "brief-1",
            "prompt": "A product shot on marble",
            "model_alias": "image-neutral-v1",
            "output_path": str(workdir / "gen.png"),
        },
    )
    assert generated.status_code == 200, generated.text
    labels = client.get("/ai-labels", params={"org_id": "org-1"})
    assert len(labels.json()) == 1
    label = labels.json()[0]
    assert label["asset_kind"] == "IMAGE"
    assert label["generator_alias"] == "image-neutral-v1"
    # brief-linked generation also enters the transformation chain
    transformations = client.get(
        "/transformation-records", params={"org_id": "org-1", "brief_id": "brief-1"}
    )
    assert len(transformations.json()) == 1


def test_route_missing_label_maps_to_404(client) -> None:
    response = client.get("/ai-labels/missing", params={"org_id": "org-1"})
    assert response.status_code == 404


def test_route_manual_metadata_idempotent(client, engine: Engine) -> None:
    payload = {
        "org_id": "org-1",
        "asset_kind": "VIDEO",
        "asset_ref": "gen/clip.mp4",
        "generator_alias": "video-neutral-v1",
        "prompt": "slow pan",
    }
    first = client.post("/generated-media-metadata", json=payload)
    second = client.post("/generated-media-metadata", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["label_id"] == second.json()["label_id"]
    assert len(client.get("/ai-labels", params={"org_id": "org-1"}).json()) == 1
