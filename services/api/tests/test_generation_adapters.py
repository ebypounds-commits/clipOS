"""Tests for image/video generation adapters (CLIP-055)."""

from __future__ import annotations

import hashlib
import shutil
import struct
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.content_planning import ContentBrief, ContentPlanningStore
from clipos_api.generation_adapters import (
    ImageGenerationRequest,
    MockImageGenAdapter,
    MockVideoGenAdapter,
    VideoGenerationRequest,
    generate_image,
    generate_video,
    prompt_color,
    solid_png,
)
from clipos_api.main import app
from clipos_api.media_ingestion import MediaAssetKind, MediaIngestionStore
from clipos_api.research import ResearchPacket, ResearchStore


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-055-generation-audit.log"
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
def store(engine: Engine, audit_log: Path):
    from clipos_api.generation_adapters import GenerationStore

    return GenerationStore.from_engine(engine)


@pytest.fixture()
def ingestion(engine: Engine) -> MediaIngestionStore:
    return MediaIngestionStore.from_engine(engine)


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


def _image_request(workdir: Path, **overrides: object):
    payload: dict[str, object] = {
        "image_asset_id": "img-1",
        "org_id": "org-1",
        "brief_id": "brief-1",
        "prompt": "A product shot on marble",
        "model_alias": "image-neutral-v1",
        "output_path": str(workdir / "gen.png"),
    }
    payload.update(overrides)
    return ImageGenerationRequest(**payload)  # type: ignore[arg-type]


def _video_request(workdir: Path, **overrides: object):
    payload: dict[str, object] = {
        "media_asset_id": "vid-1",
        "org_id": "org-1",
        "brief_id": "brief-1",
        "title": "Generated clip",
        "prompt": "slow pan over a studio desk",
        "model_alias": "video-neutral-v1",
        "duration_ms": 2000,
        "output_path": str(workdir / "gen.mp4"),
    }
    payload.update(overrides)
    return VideoGenerationRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_solid_png_is_valid() -> None:
    payload = solid_png(4, 3, (10, 20, 30))
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", payload[16:24])
    assert (width, height) == (4, 3)
    assert payload.endswith(b"IEND" + struct.pack(">I", 0xAE426082 & 0xFFFFFFFF))
    assert b"IDAT" in payload


def test_prompt_color_deterministic() -> None:
    assert prompt_color("same") == prompt_color("same")
    assert prompt_color("same") != prompt_color("different")
    assert all(0 <= channel <= 255 for channel in prompt_color("any"))


def test_mock_image_deterministic_per_prompt() -> None:
    adapter = MockImageGenAdapter()
    first = adapter.generate_image("prompt", "image-neutral-v1", 8, 8)
    second = adapter.generate_image("prompt", "image-neutral-v1", 8, 8)
    assert first == second
    other = adapter.generate_image("other prompt", "image-neutral-v1", 8, 8)
    assert first != other


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_mock_video_returns_mp4_bytes() -> None:
    adapter = MockVideoGenAdapter()
    payload = adapter.generate_video("black clip", "video-neutral-v1", 2000)
    assert payload.startswith(b"\x00\x00\x00") or b"ftyp" in payload[:32]
    assert len(payload) > 0


# ---------------------------------------------------------------------------
# Image flow
# ---------------------------------------------------------------------------


def test_generate_image_requires_brief(store, workdir: Path) -> None:
    with pytest.raises(ValueError, match="content brief not found"):
        generate_image(MockImageGenAdapter(), store, _image_request(workdir))


def test_generate_image_writes_png_and_persists(
    store, engine: Engine, workdir: Path, audit_log: Path
) -> None:
    _seed_brief(engine)
    result = generate_image(MockImageGenAdapter(), store, _image_request(workdir))
    assert result.image_asset_id == "img-1"
    assert result.model_alias == "image-neutral-v1"
    assert result.output_path == "gen.png"  # workdir-relative
    assert (workdir / "gen.png").is_file()
    assert (workdir / "gen.png").read_bytes().startswith(b"\x89PNG")
    log = audit_log.read_text(encoding="utf-8")
    assert "image_assets.generated" in log


def test_generate_image_idempotent(store, engine: Engine, workdir: Path) -> None:
    _seed_brief(engine)
    first = generate_image(MockImageGenAdapter(), store, _image_request(workdir))
    second = generate_image(MockImageGenAdapter(), store, _image_request(workdir))
    assert first.image_asset_id == second.image_asset_id
    assert len(store.list_images("org-1", "brief-1")) == 1


def test_generate_image_conflict_on_changed_prompt(store, engine: Engine, workdir: Path) -> None:
    _seed_brief(engine)
    generate_image(MockImageGenAdapter(), store, _image_request(workdir))
    with pytest.raises(ValueError, match="already generated with different fields"):
        generate_image(
            MockImageGenAdapter(), store, _image_request(workdir, prompt="Something else")
        )


def test_generate_image_path_escape(store, engine: Engine, workdir: Path) -> None:
    _seed_brief(engine)
    with pytest.raises(ValueError, match="outside render workdir"):
        generate_image(
            MockImageGenAdapter(), store, _image_request(workdir, output_path="/etc/x.png")
        )


def test_generate_image_alias_required(workdir: Path) -> None:
    with pytest.raises(ValidationError):
        ImageGenerationRequest(
            image_asset_id="img-1",
            org_id="org-1",
            brief_id="brief-1",
            prompt="p",
            model_alias="",
            output_path=str(workdir / "x.png"),
        )


# ---------------------------------------------------------------------------
# Video flow
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_generate_video_full_chain(
    store, ingestion: MediaIngestionStore, engine: Engine, workdir: Path, audit_log: Path
) -> None:
    _seed_brief(engine)
    result = generate_video(MockVideoGenAdapter(), ingestion, store, _video_request(workdir))
    assert result.media_asset_id == "vid-1"
    assert result.bytes > 0
    assert len(result.checksum_sha256) == 64
    file_bytes = (workdir / "gen.mp4").read_bytes()
    assert hashlib.sha256(file_bytes).hexdigest() == result.checksum_sha256

    asset = ingestion.get_asset("vid-1", "org-1")
    assert asset is not None
    assert asset.kind is MediaAssetKind.VIDEO
    versions = ingestion.list_versions("vid-1")
    assert len(versions) == 1
    assert versions[0].version == 1
    assert versions[0].storage_ref == "gen.mp4"
    log = audit_log.read_text(encoding="utf-8")
    assert "video_generations.generated" in log


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_generate_video_idempotent(
    store, ingestion: MediaIngestionStore, engine: Engine, workdir: Path
) -> None:
    _seed_brief(engine)
    first = generate_video(MockVideoGenAdapter(), ingestion, store, _video_request(workdir))
    second = generate_video(MockVideoGenAdapter(), ingestion, store, _video_request(workdir))
    assert first.checksum_sha256 == second.checksum_sha256
    assert len(ingestion.list_versions("vid-1")) == 1


def test_generate_video_requires_brief(
    store, ingestion: MediaIngestionStore, workdir: Path
) -> None:
    with pytest.raises(ValueError, match="content brief not found"):
        generate_video(MockVideoGenAdapter(), ingestion, store, _video_request(workdir))


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/image-generations" in paths
    assert "/video-generations" in paths


def test_router_meta() -> None:
    from clipos_api.generation_adapters import router

    assert router.tags == ["generation"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch):
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


def test_route_image_roundtrip(client, workdir: Path) -> None:
    _seed_brief_http(client)
    response = client.post(
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
    assert response.status_code == 200, response.text
    assert response.json()["output_path"] == "gen.png"

    fetched = client.get("/image-generations/img-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    listing = client.get("/image-generations", params={"org_id": "org-1", "brief_id": "brief-1"})
    assert len(listing.json()) == 1


def test_route_image_missing_brief_maps_to_404(client, workdir: Path) -> None:
    response = client.post(
        "/image-generations",
        json={
            "image_asset_id": "img-1",
            "org_id": "org-1",
            "brief_id": "missing",
            "prompt": "p",
            "model_alias": "image-neutral-v1",
            "output_path": str(workdir / "gen.png"),
        },
    )
    assert response.status_code == 404
    listing = client.get("/image-generations", params={"org_id": "org-1", "brief_id": "missing"})
    assert listing.status_code == 404
    fetched = client.get("/image-generations/missing", params={"org_id": "org-1"})
    assert fetched.status_code == 404


def test_route_image_conflict_maps_to_409(client, workdir: Path) -> None:
    _seed_brief_http(client)
    payload = {
        "image_asset_id": "img-1",
        "org_id": "org-1",
        "brief_id": "brief-1",
        "prompt": "A product shot on marble",
        "model_alias": "image-neutral-v1",
        "output_path": str(workdir / "gen.png"),
    }
    assert client.post("/image-generations", json=payload).status_code == 200
    payload["prompt"] = "changed"
    assert client.post("/image-generations", json=payload).status_code == 409


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_route_video_roundtrip(client, workdir: Path) -> None:
    _seed_brief_http(client)
    response = client.post(
        "/video-generations",
        json={
            "media_asset_id": "vid-1",
            "org_id": "org-1",
            "brief_id": "brief-1",
            "title": "Generated clip",
            "prompt": "slow pan over a studio desk",
            "model_alias": "video-neutral-v1",
            "duration_ms": 2000,
            "output_path": str(workdir / "gen.mp4"),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bytes"] > 0
    assert len(body["checksum_sha256"]) == 64

    fetched = client.get("/video-generations/vid-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert fetched.json()["storage_ref"] == "gen.mp4"


def test_route_video_missing_maps_to_404(client, workdir: Path) -> None:
    fetched = client.get("/video-generations/missing", params={"org_id": "org-1"})
    assert fetched.status_code == 404
