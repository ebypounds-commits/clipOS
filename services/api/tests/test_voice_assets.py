"""Tests for the TTS provider adapter and voice assets (CLIP-054)."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.voice_assets import (
    MockTTSAdapter,
    VoiceAsset,
    VoiceAssetStore,
    VoiceSynthesisRequest,
    silence_wav,
    synthesis_duration_ms,
    synthesize_speech,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-054-voice-assets-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> VoiceAssetStore:
    return VoiceAssetStore.from_engine(engine)


@pytest.fixture()
def workdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "render-work"
    root.mkdir()
    monkeypatch.setenv("RENDER_WORKDIR", str(root))
    return root


def _asset(voice_asset_id: str = "voice-1", **overrides: object) -> VoiceAsset:
    payload: dict[str, object] = {
        "voice_asset_id": voice_asset_id,
        "org_id": "org-1",
        "label": "Fixture voice",
        "voice_ref": "neutral-feminine-01",
        "model_alias": "tts-neutral-v1",
    }
    payload.update(overrides)
    return VoiceAsset(**payload)  # type: ignore[arg-type]


def _request(**overrides: object) -> VoiceSynthesisRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "text": "Hello ClipOS",
        "output_path": "",
    }
    payload.update(overrides)
    return VoiceSynthesisRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure helpers and adapter
# ---------------------------------------------------------------------------


def test_synthesis_duration_bounds() -> None:
    assert synthesis_duration_ms("") == 500  # empty clamped up (pydantic blocks it anyway)
    assert synthesis_duration_ms("Hi") == 500
    assert synthesis_duration_ms("x" * 15) == 1000
    assert synthesis_duration_ms("x" * 10_000) == 15_000


def test_silence_wav_is_valid() -> None:
    payload = silence_wav(1000)
    with wave.open(io.BytesIO(payload), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 800
        assert wav.getnframes() == 800


def test_mock_adapter_deterministic_per_text() -> None:
    adapter = MockTTSAdapter()
    first = adapter.synthesize("Same text", "voice-ref", "tts-neutral-v1")
    second = adapter.synthesize("Same text", "voice-ref", "tts-neutral-v1")
    assert first == second
    longer = adapter.synthesize("Same text plus a lot more characters here", "voice-ref", "x")
    assert len(longer) > len(first)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_register_asset_idempotent(store: VoiceAssetStore) -> None:
    first = store.register_asset(_asset())
    second = store.register_asset(_asset())
    assert first.voice_asset_id == second.voice_asset_id
    assert first.model_alias == "tts-neutral-v1"
    assert len(store.list_assets("org-1")) == 1


def test_register_asset_conflict_on_different_fields(store: VoiceAssetStore) -> None:
    store.register_asset(_asset())
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_asset(_asset(label="Different label"))


def test_get_asset_org_scoped(store: VoiceAssetStore) -> None:
    store.register_asset(_asset())
    assert store.get_asset("voice-1", "org-1") is not None
    assert store.get_asset("voice-1", "org-other") is None
    assert store.get_asset("missing", "org-1") is None


def test_register_asset_audit(store: VoiceAssetStore, audit_log: Path) -> None:
    store.register_asset(_asset())
    log = audit_log.read_text(encoding="utf-8")
    assert "voice_assets.registered" in log


def test_model_alias_required_by_pydantic() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VoiceAsset(
            voice_asset_id="voice-1",
            org_id="org-1",
            label="Fixture",
            voice_ref="ref",
            model_alias="",
        )


# ---------------------------------------------------------------------------
# Synthesis flow
# ---------------------------------------------------------------------------


def test_synthesize_writes_wav_and_audits(
    store: VoiceAssetStore, workdir: Path, audit_log: Path
) -> None:
    store.register_asset(_asset())
    output = workdir / "voice-out.wav"
    result = synthesize_speech(
        MockTTSAdapter(),
        store,
        "voice-1",
        _request(output_path=str(output)),
    )
    assert result.bytes_written > 0
    assert result.duration_ms == synthesis_duration_ms("Hello ClipOS")
    assert result.model_alias == "tts-neutral-v1"
    assert output.is_file()
    with wave.open(io.BytesIO(output.read_bytes()), "rb") as wav:
        assert wav.getnframes() > 0
    log = audit_log.read_text(encoding="utf-8")
    assert "voice_synthesis.completed" in log
    assert "tts-neutral-v1" in log


def test_synthesize_missing_asset_raises(store: VoiceAssetStore, workdir: Path) -> None:
    with pytest.raises(ValueError, match="voice asset not found"):
        synthesize_speech(
            MockTTSAdapter(),
            store,
            "missing",
            _request(output_path=str(workdir / "x.wav")),
        )


def test_synthesize_path_escape_raises(store: VoiceAssetStore, workdir: Path) -> None:
    store.register_asset(_asset())
    with pytest.raises(ValueError, match="outside render workdir"):
        synthesize_speech(
            MockTTSAdapter(),
            store,
            "voice-1",
            _request(output_path="/etc/evil.wav"),
        )


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/voice-assets" in paths
    assert "/voice-assets/{voice_asset_id}" in paths
    assert "/voice-assets/{voice_asset_id}/synthesize" in paths


def test_router_meta() -> None:
    from clipos_api.voice_assets import router

    assert router.tags == ["voice-assets"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def test_route_register_fetch_synthesize_roundtrip(client, workdir: Path) -> None:
    created = client.post(
        "/voice-assets",
        json={
            "voice_asset_id": "voice-1",
            "org_id": "org-1",
            "label": "Fixture voice",
            "voice_ref": "neutral-feminine-01",
            "model_alias": "tts-neutral-v1",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["model_alias"] == "tts-neutral-v1"

    fetched = client.get("/voice-assets/voice-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    listing = client.get("/voice-assets", params={"org_id": "org-1"})
    assert len(listing.json()) == 1

    output = workdir / "route-voice.wav"
    synthesized = client.post(
        "/voice-assets/voice-1/synthesize",
        json={
            "org_id": "org-1",
            "text": "Hello ClipOS route",
            "output_path": str(output),
        },
    )
    assert synthesized.status_code == 200, synthesized.text
    body = synthesized.json()
    assert body["bytes_written"] > 0
    assert body["voice_asset_id"] == "voice-1"
    assert output.is_file()


def test_route_missing_asset_maps_to_404(client, workdir: Path) -> None:
    fetched = client.get("/voice-assets/missing", params={"org_id": "org-1"})
    assert fetched.status_code == 404
    synthesized = client.post(
        "/voice-assets/missing/synthesize",
        json={
            "org_id": "org-1",
            "text": "Hello",
            "output_path": str(workdir / "x.wav"),
        },
    )
    assert synthesized.status_code == 404


def test_route_conflict_maps_to_409(client) -> None:
    payload = {
        "voice_asset_id": "voice-1",
        "org_id": "org-1",
        "label": "Fixture voice",
        "voice_ref": "neutral-feminine-01",
        "model_alias": "tts-neutral-v1",
    }
    assert client.post("/voice-assets", json=payload).status_code == 200
    payload["label"] = "Renamed"
    assert client.post("/voice-assets", json=payload).status_code == 409


def test_route_synthesize_path_escape_maps_to_422(client) -> None:
    client.post(
        "/voice-assets",
        json={
            "voice_asset_id": "voice-1",
            "org_id": "org-1",
            "label": "Fixture voice",
            "voice_ref": "neutral-feminine-01",
            "model_alias": "tts-neutral-v1",
        },
    )
    response = client.post(
        "/voice-assets/voice-1/synthesize",
        json={"org_id": "org-1", "text": "Hello", "output_path": "/etc/evil.wav"},
    )
    assert response.status_code == 422
