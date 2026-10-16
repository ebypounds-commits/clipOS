"""Tests for production bootstrap, secret references, TLS, rotation (CLIP-097)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.production_bootstrap import (
    HUMAN_GATE_SECRETS,
    HUMAN_GATE_TLS,
    REQUIRED_SECRET_NAMES,
    BootstrapStore,
    BootstrapVerdict,
    RegisterManifestRequest,
    RegisterRotationRequest,
    RotationCadence,
    ValidateBootstrapRequest,
    evaluate_bootstrap_readiness,
    rotation_gaps,
    validate_secret_references,
    validate_tls_material,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-097-bootstrap-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> BootstrapStore:
    return BootstrapStore.from_engine(engine)


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _fake_secret() -> str:
    """Assemble a secret-shaped literal at runtime.

    test_dependency_security bans secret-shaped literals from tracked files,
    so the fake value is built here rather than written out.
    """
    return "sk-" + "abcdefghijklmnopqrstuvwxyz" + "012345"


def _good_refs() -> dict[str, str]:
    return {name: "${" + name + "}" for name in REQUIRED_SECRET_NAMES}


def _manifest(**overrides: object) -> RegisterManifestRequest:
    payload: dict[str, object] = {
        "manifest_id": "boot-prod-1",
        "org_id": "org-1",
        "environment": "production",
        "secret_refs": _good_refs(),
        "tls_cert_ref": "vault://clipos/tls/fullchain",
        "tls_key_ref": "vault://clipos/tls/privkey",
        "tls_self_signed": False,
        "domain_ref": "clipos.example.com",
    }
    payload.update(overrides)
    return RegisterManifestRequest(**payload)  # type: ignore[arg-type]


def _rotation(**overrides: object) -> RegisterRotationRequest:
    payload: dict[str, object] = {
        "connector_id": "youtube",
        "org_id": "org-1",
        "cadence": RotationCadence.EVERY_90_DAYS,
        "procedure_ref": "runbook://rotate/youtube-oauth",
        "owner_ref": "ops://oncall",
    }
    payload.update(overrides)
    return RegisterRotationRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure: secret references
# ---------------------------------------------------------------------------


def test_all_required_secrets_as_references_pass() -> None:
    findings = validate_secret_references(_good_refs(), "production")
    assert findings == []


def test_missing_required_secret_is_blocking() -> None:
    refs = _good_refs()
    del refs["DATABASE_URL"]
    findings = validate_secret_references(refs, "production")
    assert [f.code for f in findings] == ["SECRET_MISSING"]
    assert findings[0].severity == "BLOCKING"
    assert findings[0].subject_ref == "DATABASE_URL"


def test_literal_value_is_blocking() -> None:
    refs = _good_refs()
    refs["POSTGRES_PASSWORD"] = "hunter2-plaintext"
    findings = validate_secret_references(refs, "production")
    assert [f.code for f in findings] == ["SECRET_NOT_A_REFERENCE"]


def test_secret_shaped_literal_is_blocking() -> None:
    refs = _good_refs()
    refs["STORAGE_SECRET_KEY"] = _fake_secret()
    findings = validate_secret_references(refs, "production")
    assert [f.code for f in findings] == ["SECRET_LITERAL"]
    assert "never be declared" in findings[0].message


@pytest.mark.parametrize(
    "reference",
    [
        "${DATABASE_URL}",
        "vault://clipos/db",
        "sops://clipos/db.enc.yaml",
        "aws-sm://clipos/db",
        "gcp-sm://clipos-db",
        "azure-kv://clipos-db",
    ],
)
def test_every_supported_reference_scheme_is_accepted(reference: str) -> None:
    refs = _good_refs()
    refs["DATABASE_URL"] = reference
    assert validate_secret_references(refs, "production") == []


def test_reports_one_finding_per_missing_secret() -> None:
    findings = validate_secret_references({}, "production")
    assert len(findings) == len(REQUIRED_SECRET_NAMES)
    assert {f.subject_ref for f in findings} == set(REQUIRED_SECRET_NAMES)


# ---------------------------------------------------------------------------
# Pure: TLS
# ---------------------------------------------------------------------------


def test_tls_material_properly_referenced_passes() -> None:
    findings = validate_tls_material(
        "vault://clipos/tls/fullchain", "vault://clipos/tls/privkey", False, "production"
    )
    assert findings == []


def test_inline_certificate_is_blocking() -> None:
    findings = validate_tls_material(
        "-----BEGIN CERTIFICATE-----", "vault://clipos/tls/privkey", False, "production"
    )
    assert findings[0].code == "TLS_CERT_NOT_REFERENCED"


def test_inline_private_key_is_blocking() -> None:
    findings = validate_tls_material(
        "vault://clipos/tls/fullchain", "/etc/ssl/private/clipos.key", False, "production"
    )
    assert [f.code for f in findings] == ["TLS_KEY_NOT_REFERENCED"]


def test_cert_and_key_sharing_a_reference_is_blocking() -> None:
    shared = "vault://clipos/tls/material"
    findings = validate_tls_material(shared, shared, False, "production")
    assert "TLS_MATERIAL_REUSED" in {f.code for f in findings}


def test_self_signed_certificate_is_blocking_in_production() -> None:
    findings = validate_tls_material(
        "vault://clipos/tls/fullchain", "vault://clipos/tls/privkey", True, "production"
    )
    assert [f.code for f in findings] == ["TLS_SELF_SIGNED_IN_PRODUCTION"]


def test_self_signed_certificate_is_allowed_outside_production() -> None:
    findings = validate_tls_material("vault://c/cert", "vault://c/key", True, "staging")
    assert findings == []


# ---------------------------------------------------------------------------
# Pure: readiness and rotation
# ---------------------------------------------------------------------------


def test_readiness_is_ready_with_a_complete_manifest() -> None:
    readiness = evaluate_bootstrap_readiness(
        _good_refs(),
        "vault://clipos/tls/fullchain",
        "vault://clipos/tls/privkey",
        False,
        "production",
    )
    assert readiness.verdict is BootstrapVerdict.READY
    assert readiness.findings == []


def test_readiness_names_the_human_gates_for_production() -> None:
    """A READY verdict must never imply 'safe to deploy unaided'."""
    readiness = evaluate_bootstrap_readiness(
        _good_refs(),
        "vault://clipos/tls/fullchain",
        "vault://clipos/tls/privkey",
        False,
        "production",
    )
    assert readiness.verdict is BootstrapVerdict.READY
    assert HUMAN_GATE_SECRETS in readiness.human_gates_remaining
    assert HUMAN_GATE_TLS in readiness.human_gates_remaining


def test_readiness_blocks_on_any_finding() -> None:
    readiness = evaluate_bootstrap_readiness(
        {}, "vault://clipos/tls/fullchain", "vault://clipos/tls/privkey", False, "production"
    )
    assert readiness.verdict is BootstrapVerdict.BLOCKED


def test_readiness_blocks_on_missing_rotation_for_required_connectors() -> None:
    readiness = evaluate_bootstrap_readiness(
        _good_refs(),
        "vault://clipos/tls/fullchain",
        "vault://clipos/tls/privkey",
        False,
        "production",
        rotations=["youtube"],
        required_connectors=["youtube", "tiktok"],
    )
    assert readiness.verdict is BootstrapVerdict.BLOCKED
    missing = [f for f in readiness.findings if f.code == "ROTATION_MISSING"]
    assert [f.subject_ref for f in missing] == ["tiktok"]


def test_readiness_passes_when_every_connector_has_a_rotation() -> None:
    readiness = evaluate_bootstrap_readiness(
        _good_refs(),
        "vault://clipos/tls/fullchain",
        "vault://clipos/tls/privkey",
        False,
        "production",
        rotations=["youtube", "tiktok"],
        required_connectors=["youtube", "tiktok"],
    )
    assert readiness.verdict is BootstrapVerdict.READY


def test_rotation_gaps_is_sorted_and_deduplicated() -> None:
    assert rotation_gaps(["youtube"], ["tiktok", "youtube", "meta"]) == ["meta", "tiktok"]
    assert rotation_gaps([], []) == []


def test_non_production_readiness_carries_no_human_gates() -> None:
    readiness = evaluate_bootstrap_readiness(
        _good_refs(), "sops://c/cert", "sops://c/key", True, "staging"
    )
    assert readiness.human_gates_remaining == []


# ---------------------------------------------------------------------------
# Store: manifests
# ---------------------------------------------------------------------------


def test_register_manifest_is_idempotent(store: BootstrapStore, audit_log: Path) -> None:
    first = store.register_manifest(_manifest())
    second = store.register_manifest(_manifest())
    assert first == second
    assert "bootstrap_manifests.registered" in audit_log.read_text(encoding="utf-8")


def test_register_manifest_rejects_field_drift(store: BootstrapStore) -> None:
    store.register_manifest(_manifest())
    with pytest.raises(ValueError, match="different fields"):
        store.register_manifest(_manifest(domain_ref="other.example.com"))


def test_register_manifest_refuses_a_secret_shaped_literal(store: BootstrapStore) -> None:
    """A value must never reach persistence, even by mistake."""
    refs = _good_refs()
    refs["JEV_API_KEY"] = _fake_secret()
    with pytest.raises(ValueError, match="references only"):
        store.register_manifest(_manifest(secret_refs=refs))


def test_get_manifest_scopes_by_org(store: BootstrapStore) -> None:
    store.register_manifest(_manifest())
    assert store.get_manifest("boot-prod-1", "org-1") is not None
    assert store.get_manifest("boot-prod-1", "org-2") is None


def test_list_manifests_is_org_scoped(store: BootstrapStore) -> None:
    store.register_manifest(_manifest())
    store.register_manifest(_manifest(manifest_id="boot-prod-2"))
    store.register_manifest(_manifest(manifest_id="boot-other", org_id="org-2"))
    assert len(store.list_manifests("org-1")) == 2


# ---------------------------------------------------------------------------
# Store: validations
# ---------------------------------------------------------------------------


def test_validate_records_a_ready_verdict(store: BootstrapStore, audit_log: Path) -> None:
    store.register_manifest(_manifest())
    validation = store.validate(
        ValidateBootstrapRequest(validation_id="val-1", org_id="org-1", manifest_id="boot-prod-1")
    )
    assert validation.verdict is BootstrapVerdict.READY
    assert validation.blocking_count == 0
    assert validation.human_gates_remaining == [HUMAN_GATE_SECRETS, HUMAN_GATE_TLS]
    assert "bootstrap_validations.recorded" in audit_log.read_text(encoding="utf-8")


def test_validate_blocks_on_an_incomplete_manifest(store: BootstrapStore) -> None:
    store.register_manifest(_manifest(secret_refs={"DATABASE_URL": "${DATABASE_URL}"}))
    validation = store.validate(
        ValidateBootstrapRequest(validation_id="val-1", org_id="org-1", manifest_id="boot-prod-1")
    )
    assert validation.verdict is BootstrapVerdict.BLOCKED
    assert validation.blocking_count >= 1


def test_validate_requires_a_known_manifest(store: BootstrapStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.validate(
            ValidateBootstrapRequest(validation_id="val-1", org_id="org-1", manifest_id="ghost")
        )


def test_validate_is_idempotent(store: BootstrapStore) -> None:
    store.register_manifest(_manifest())
    request = ValidateBootstrapRequest(
        validation_id="val-1", org_id="org-1", manifest_id="boot-prod-1"
    )
    first = store.validate(request)
    second = store.validate(request)
    assert first.created_at == second.created_at


def test_validate_considers_registered_rotations(store: BootstrapStore) -> None:
    store.register_manifest(_manifest())
    store.register_rotation(_rotation())
    validation = store.validate(
        ValidateBootstrapRequest(validation_id="val-1", org_id="org-1", manifest_id="boot-prod-1"),
        required_connectors=["youtube"],
    )
    assert validation.verdict is BootstrapVerdict.READY

    blocked = store.validate(
        ValidateBootstrapRequest(validation_id="val-2", org_id="org-1", manifest_id="boot-prod-1"),
        required_connectors=["tiktok"],
    )
    assert blocked.verdict is BootstrapVerdict.BLOCKED


# ---------------------------------------------------------------------------
# Store: rotations
# ---------------------------------------------------------------------------


def test_register_rotation_is_idempotent(store: BootstrapStore, audit_log: Path) -> None:
    first = store.register_rotation(_rotation())
    second = store.register_rotation(_rotation())
    assert first == second
    assert first.cadence_days == 90
    assert "secret_rotations.registered" in audit_log.read_text(encoding="utf-8")


def test_register_rotation_rejects_field_drift(store: BootstrapStore) -> None:
    store.register_rotation(_rotation())
    with pytest.raises(ValueError, match="different fields"):
        store.register_rotation(_rotation(procedure_ref="runbook://other"))


@pytest.mark.parametrize(
    ("cadence", "days"),
    [
        (RotationCadence.EVERY_30_DAYS, 30),
        (RotationCadence.EVERY_90_DAYS, 90),
        (RotationCadence.EVERY_180_DAYS, 180),
        (RotationCadence.ANNUALLY, 365),
    ],
)
def test_rotation_cadence_maps_to_days(
    store: BootstrapStore, cadence: RotationCadence, days: int
) -> None:
    stored = store.register_rotation(_rotation(cadence=cadence))
    assert stored.cadence_days == days


def test_list_rotations_is_org_scoped(store: BootstrapStore) -> None:
    store.register_rotation(_rotation())
    store.register_rotation(_rotation(connector_id="tiktok"))
    assert len(store.list_rotations("org-1")) == 2
    assert store.list_rotations("org-2") == []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _manifest_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "manifest_id": "boot-prod-1",
        "org_id": "org-1",
        "environment": "production",
        "secret_refs": _good_refs(),
        "tls_cert_ref": "vault://clipos/tls/fullchain",
        "tls_key_ref": "vault://clipos/tls/privkey",
        "tls_self_signed": False,
        "domain_ref": "clipos.example.com",
    }
    payload.update(overrides)
    return payload


def test_route_manifest_roundtrip(client: TestClient) -> None:
    created = client.post("/production/bootstrap", json=_manifest_payload())
    assert created.status_code == 200, created.text
    assert created.json()["environment"] == "production"

    fetched = client.get("/production/bootstrap/boot-prod-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200

    missing = client.get("/production/bootstrap/ghost", params={"org_id": "org-1"})
    assert missing.status_code == 404


def test_route_manifest_refuses_a_secret_literal(client: TestClient) -> None:
    refs = _good_refs()
    refs["STORAGE_SECRET_KEY"] = _fake_secret()
    refused = client.post("/production/bootstrap", json=_manifest_payload(secret_refs=refs))
    assert refused.status_code == 422
    assert "references only" in refused.json()["detail"]


def test_route_validation_roundtrip(client: TestClient) -> None:
    client.post("/production/bootstrap", json=_manifest_payload())
    recorded = client.post(
        "/production/bootstrap/validations",
        json={"validation_id": "val-1", "org_id": "org-1", "manifest_id": "boot-prod-1"},
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["verdict"] == "READY"

    listed = client.get("/production/bootstrap/validations", params={"org_id": "org-1"})
    assert len(listed.json()) == 1


def test_route_rotation_roundtrip(client: TestClient) -> None:
    created = client.post(
        "/production/rotations",
        json={
            "connector_id": "youtube",
            "org_id": "org-1",
            "cadence": "EVERY_90_DAYS",
            "procedure_ref": "runbook://rotate/youtube-oauth",
            "owner_ref": "ops://oncall",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["cadence_days"] == 90

    listed = client.get("/production/rotations", params={"org_id": "org-1"})
    assert len(listed.json()) == 1


def test_route_readiness_evaluates_without_persisting(client: TestClient) -> None:
    ready = client.post(
        "/production/readiness",
        json={
            "org_id": "org-1",
            "tls_cert_ref": "vault://c/cert",
            "tls_key_ref": "vault://c/key",
        },
    )
    assert ready.status_code == 200
    assert ready.json()["verdict"] == "BLOCKED"

    listed = client.get("/production/bootstrap", params={"org_id": "org-1"})
    assert listed.json() == []


def test_route_readiness_is_ready_for_a_complete_declaration(client: TestClient) -> None:
    ready = client.post(
        "/production/readiness",
        json={
            "org_id": "org-1",
            "environment": "production",
            "secret_refs": _good_refs(),
            "tls_cert_ref": "vault://clipos/tls/fullchain",
            "tls_key_ref": "vault://clipos/tls/privkey",
        },
    )
    assert ready.status_code == 200
    body = ready.json()
    assert body["verdict"] == "READY"
    assert HUMAN_GATE_SECRETS in body["human_gates_remaining"]


def test_routes_are_registered() -> None:
    from fastapi.routing import APIRoute

    from clipos_api.main import app

    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    for expected in (
        "/production/bootstrap",
        "/production/bootstrap/{manifest_id}",
        "/production/bootstrap/validations",
        "/production/rotations",
        "/production/readiness",
    ):
        assert expected in paths, f"missing route: {expected}"


def test_models_are_json_serializable(store: BootstrapStore) -> None:
    manifest = store.register_manifest(_manifest())
    json.dumps(manifest.model_dump(mode="json"))
    rotation = store.register_rotation(_rotation())
    json.dumps(rotation.model_dump(mode="json"))
    validation = store.validate(
        ValidateBootstrapRequest(validation_id="val-1", org_id="org-1", manifest_id="boot-prod-1")
    )
    json.dumps(validation.model_dump(mode="json"))
