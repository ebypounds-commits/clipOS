"""Tests for provenance manifests and content fingerprints (CLIP-032)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit, provenance, rights_entities
from clipos_api.main import app
from clipos_api.provenance import (
    Algorithm,
    ContentFingerprint,
    GenerateManifestRequest,
    ProvenanceStep,
    ProvenanceStore,
    StepAppendRequest,
    StepType,
)
from clipos_api.rights_entities import ContentSource, Creator, SourceItem, SourceType


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    rights_entities.Base.metadata.create_all(e)
    provenance.Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-032-provenance-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ProvenanceStore:
    entities = rights_entities.RightsEntitiesStore.from_engine(engine)
    entities.register_creator(_creator())
    entities.register_source(_source())
    entities.register_item(_item())
    return ProvenanceStore.from_engine(engine)


def _creator(org_id: str = "org-1", creator_id: str = "creator-1") -> Creator:
    return Creator.model_validate(
        {
            "creator_id": creator_id,
            "org_id": org_id,
            "display_name": "Ada Lovelace",
            "contact_ref": "secrets://contacts/creator-1",
        }
    )


def _source(source_id: str = "source-1", org_id: str = "org-1") -> ContentSource:
    return ContentSource.model_validate(
        {
            "source_id": source_id,
            "org_id": org_id,
            "creator_id": "creator-1",
            "source_type": SourceType.FEED,
            "canonical_url": "https://example.com/feed.xml",
        }
    )


def _item(item_id: str = "item-1", org_id: str = "org-1", **changes: object) -> SourceItem:
    values = {
        "item_id": item_id,
        "org_id": org_id,
        "source_id": "source-1",
        "external_ref": f"ext:{item_id}",
        "captured_at": datetime(2026, 1, 1, tzinfo=UTC),
        "content_ref": f"storage://source-items/{item_id}",
    }
    values.update(changes)
    return SourceItem.model_validate(values)


def _step(step_type: StepType = StepType.CAPTURED, **changes: object) -> ProvenanceStep:
    values = {
        "step_type": step_type,
        "actor_ref": "actor://ingest-worker-1",
        "at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "inputs_refs": ["storage://source-items/item-1"],
        "outputs_ref": "storage://derived/item-1",
        "transform_note": "initial capture",
    }
    values.update(changes)
    return ProvenanceStep.model_validate(values)


def _append(store: ProvenanceStore, item_id: str = "item-1", **changes: object) -> None:
    store.append_step(item_id, "org-1", _step(**changes))


def _fingerprint(fingerprint_id: str = "fp-1", **changes: object) -> ContentFingerprint:
    values = {
        "fingerprint_id": fingerprint_id,
        "manifest_id": "prov-item-1",
        "org_id": "org-1",
        "algorithm": Algorithm.SHA256,
        "fingerprint_hex": sha256(b"artifact").hexdigest(),
        "artifact_ref": "storage://artifacts/item-1",
        "computed_at": datetime(2026, 1, 3, tzinfo=UTC),
    }
    values.update(changes)
    return ContentFingerprint.model_validate(values)


def test_append_step_validates_item_and_org(store: ProvenanceStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.append_step("missing", "org-1", _step())
    with pytest.raises(ValueError, match="different organization"):
        store.append_step("item-1", "org-2", _step())


def test_manifest_hash_is_deterministic(store: ProvenanceStore) -> None:
    _append(store)
    _append(store, step_type=StepType.TRANSFORMED, transform_note="trim")
    first = store.generate_manifest("item-1", "org-1")
    expected = sha256(
        provenance.canonical_json(
            provenance.canonical_manifest_payload(first.model_copy(update={"manifest_hash": None}))
        ).encode()
    ).hexdigest()
    assert first.manifest_hash == expected
    second = store.generate_manifest("item-1", "org-1")
    assert second.manifest_hash == first.manifest_hash
    assert [step.step_type for step in second.chain] == [
        StepType.CAPTURED,
        StepType.TRANSFORMED,
    ]


def test_fingerprint_is_idempotent_per_manifest_and_algorithm(store: ProvenanceStore) -> None:
    _append(store)
    store.generate_manifest("item-1", "org-1")
    first = store.register_fingerprint(_fingerprint())
    repeat = store.register_fingerprint(_fingerprint("fp-1-dup"))
    assert repeat.fingerprint_id == first.fingerprint_id
    with pytest.raises(ValueError, match="different fields"):
        store.register_fingerprint(
            _fingerprint(
                "fp-1-conflict",
                fingerprint_hex=sha256(b"other").hexdigest(),
            )
        )
    with pytest.raises(ValueError, match="not found"):
        store.register_fingerprint(_fingerprint("fp-x", manifest_id="prov-missing"))
    with pytest.raises(ValueError, match="different organization"):
        store.register_fingerprint(_fingerprint("fp-y", org_id="org-2"))


def test_fingerprint_hex_length_and_charset_validation(store: ProvenanceStore) -> None:
    _append(store)
    store.generate_manifest("item-1", "org-1")
    bad_lengths = {
        Algorithm.SHA256: "z" * 64,
        Algorithm.SHA1: "a" * 41,
        Algorithm.MD5: "a" * 31,
    }
    for algorithm, value in bad_lengths.items():
        with pytest.raises(ValueError, match="fingerprint_hex"):
            _fingerprint(f"fp-{algorithm.value}", algorithm=algorithm, fingerprint_hex=value)
    with pytest.raises(ValueError, match="hexadecimal"):
        _fingerprint("fp-bad", fingerprint_hex="g" * 64)


def test_steps_are_append_only_and_manifest_requires_steps(store: ProvenanceStore) -> None:
    _append(store)
    manifest = store.get_manifest("prov-item-1", "org-1")
    assert len(manifest.chain) == 1
    first = store.generate_manifest("item-1", "org-1")
    assert first.manifest_hash is not None
    _append(store, step_type=StepType.PUBLISHED, transform_note=None)
    updated = store.get_manifest("prov-item-1", "org-1")
    assert len(updated.chain) == 2
    assert updated.manifest_hash is None


def test_audited_events(store: ProvenanceStore, audit_log: Path) -> None:
    _append(store)
    store.generate_manifest("item-1", "org-1")
    store.register_fingerprint(_fingerprint())
    text = audit_log.read_text(encoding="utf-8")
    assert "provenance.step_appended" in text
    assert "provenance.manifest_generated" in text
    assert "provenance.fingerprint_registered" in text


def test_org_scoped_listings(store: ProvenanceStore) -> None:
    engine = store._sessions.kw["bind"]
    entities = rights_entities.RightsEntitiesStore.from_engine(engine)
    entities.register_creator(_creator("org-2", "creator-2"))
    entities.register_source(
        _source("source-2", "org-2").__class__.model_validate(
            {**_source("source-2", "org-2").model_dump(), "creator_id": "creator-2"}
        )
    )
    entities.register_item(_item("item-2", "org-2", source_id="source-2"))
    _append(store)
    store.append_step("item-2", "org-2", _step())
    assert [m.manifest_id for m in store.list_manifests("org-1")] == ["prov-item-1"]
    assert [m.manifest_id for m in store.list_manifests("org-2")] == ["prov-item-2"]
    store.generate_manifest("item-1", "org-1")
    store.register_fingerprint(_fingerprint())
    assert len(store.list_fingerprints("prov-item-1", "org-1")) == 1
    with pytest.raises(ValueError, match="not found"):
        store.list_fingerprints("prov-item-1", "org-2")
    with pytest.raises(ValueError, match="not found"):
        store.get_manifest("prov-item-1", "org-2")


def test_routes_are_wired() -> None:
    names = {route.name for route in app.routes if isinstance(route, APIRoute)}
    assert {
        "append_provenance_step",
        "generate_provenance_manifest",
        "get_provenance_manifest",
        "register_content_fingerprint",
        "list_content_fingerprints",
    } <= names


def test_no_provider_model_identifiers_or_inline_secrets() -> None:
    src = Path(provenance.__file__).read_text()
    for token in ("gpt-", "claude-", "gemini-", "deepseek", "qwen", "sk-", "password="):
        assert token not in src, token


def test_step_request_rejects_unknown_step_type() -> None:
    with pytest.raises(ValueError):
        StepAppendRequest.model_validate(
            {
                "org_id": "org-1",
                "step": {
                    "step_type": "teleported",
                    "actor_ref": "actor://x",
                    "at": datetime(2026, 1, 2, tzinfo=UTC).isoformat(),
                    "outputs_ref": "storage://x",
                },
            }
        )


def test_generate_request_requires_org() -> None:
    with pytest.raises(ValueError):
        GenerateManifestRequest.model_validate({"org_id": ""})


def test_canonical_json_is_sorted_and_compact() -> None:
    payload = {"b": 1, "a": {"d": [1, 2], "c": "x"}}
    assert provenance.canonical_json(payload) == json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
