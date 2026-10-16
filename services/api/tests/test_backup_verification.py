"""Tests for encrypted backup and restore verification (CLIP-096)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.backup_crypto import (
    ALG,
    DEFAULT_KEY_ENV,
    BackupCryptoError,
    EncryptedEnvelope,
    decrypt_backup,
    decrypt_blob,
    encrypt_backup,
    generate_key,
    key_fingerprint,
    load_key,
)
from clipos_api.backup_verification import (
    DEFAULT_RESTORE_RTO_SECONDS,
    ArtifactKind,
    BackupStore,
    MonitorStatus,
    RecordArtifactRequest,
    RecordRestoreTestRequest,
    RestoreOutcome,
    check_backup_freshness,
    evaluate_drill_cadence,
    evaluate_restore,
    restore_test_due,
    summarize_backup_status,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-096-backup-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> BackupStore:
    return BackupStore.from_engine(engine)


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


@pytest.fixture()
def key() -> bytes:
    return bytes.fromhex(generate_key())


# ---------------------------------------------------------------------------
# Encryption envelope
# ---------------------------------------------------------------------------


def test_roundtrip_reproduces_the_plaintext(key: bytes) -> None:
    payload = b"clipos backup payload" * 50
    envelope = encrypt_backup(payload, key, artifact_ref="artifact-1")
    assert decrypt_backup(envelope, key) == payload
    assert decrypt_blob(envelope.to_bytes(), key) == payload


def test_plaintext_does_not_appear_in_the_envelope(key: bytes) -> None:
    payload = b"SECRET-CANARY-VALUE-1234567890"
    envelope = encrypt_backup(payload, key, artifact_ref="artifact-1")
    blob = envelope.to_bytes()
    assert payload not in blob
    assert b"SECRET-CANARY" not in blob


def test_envelope_header_carries_the_versioned_alg(key: bytes) -> None:
    envelope = encrypt_backup(b"data", key, artifact_ref="artifact-1")
    assert envelope.header["alg"] == ALG
    assert envelope.header["version"] == 1
    assert envelope.header["key_ref"] == DEFAULT_KEY_ENV
    assert envelope.header["key_fingerprint"] == key_fingerprint(key)
    assert envelope.header["plaintext_bytes"] == 4


def test_ciphertext_tampering_is_detected(key: bytes) -> None:
    blob = bytearray(encrypt_backup(b"important data", key, artifact_ref="a").to_bytes())
    blob[-1] ^= 0x01
    with pytest.raises(BackupCryptoError, match="failed authentication"):
        decrypt_blob(bytes(blob), key)


def test_header_tampering_is_detected(key: bytes) -> None:
    envelope = encrypt_backup(b"important data", key, artifact_ref="a")
    tampered = dict(envelope.header)
    tampered["artifact_ref"] = "b"
    forged = EncryptedEnvelope(header=tampered, ciphertext=envelope.ciphertext)
    with pytest.raises(BackupCryptoError, match="failed authentication"):
        decrypt_backup(forged, key)


def test_truncated_envelope_is_rejected(key: bytes) -> None:
    blob = encrypt_backup(b"important data", key, artifact_ref="a").to_bytes()
    with pytest.raises(BackupCryptoError, match="truncated|authentication"):
        decrypt_blob(blob[: len(blob) - 5], key)


def test_wrong_key_is_rejected(key: bytes) -> None:
    blob = encrypt_backup(b"important data", key, artifact_ref="a").to_bytes()
    other = bytes.fromhex(generate_key())
    with pytest.raises(BackupCryptoError, match="failed authentication"):
        decrypt_blob(blob, other)


def test_bad_magic_is_rejected(key: bytes) -> None:
    with pytest.raises(BackupCryptoError, match="bad magic"):
        EncryptedEnvelope.from_bytes(b"NOTCLIPOS" + b"\x00" * 40)


def test_missing_tag_is_rejected(key: bytes) -> None:
    envelope = encrypt_backup(b"data", key, artifact_ref="a")
    header = dict(envelope.header)
    del header["tag"]
    with pytest.raises(BackupCryptoError, match="missing its authentication tag"):
        decrypt_backup(EncryptedEnvelope(header=header, ciphertext=envelope.ciphertext), key)


def test_unsupported_alg_is_rejected(key: bytes) -> None:
    envelope = encrypt_backup(b"data", key, artifact_ref="a")
    header = dict(envelope.header)
    header["alg"] = "rot13"
    with pytest.raises(BackupCryptoError, match="unsupported envelope alg"):
        decrypt_backup(EncryptedEnvelope(header=header, ciphertext=envelope.ciphertext), key)


def test_empty_backup_is_refused(key: bytes) -> None:
    with pytest.raises(BackupCryptoError, match="refusing to encrypt an empty backup"):
        encrypt_backup(b"", key, artifact_ref="a")


def test_nonce_differs_between_encryptions(key: bytes) -> None:
    first = encrypt_backup(b"same data", key, artifact_ref="a")
    second = encrypt_backup(b"same data", key, artifact_ref="a")
    assert first.header["nonce"] != second.header["nonce"]
    assert first.ciphertext != second.ciphertext


def test_load_key_requires_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    with pytest.raises(BackupCryptoError, match="not configured"):
        load_key()


def test_load_key_rejects_bad_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_KEY_ENV, "not-hex")
    with pytest.raises(BackupCryptoError, match="not valid hex"):
        load_key()


def test_load_key_rejects_wrong_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_KEY_ENV, "ab" * 16)
    with pytest.raises(BackupCryptoError, match="must be 32 bytes"):
        load_key()


def test_load_key_accepts_a_generated_key(monkeypatch: pytest.MonkeyPatch) -> None:
    generated = generate_key()
    monkeypatch.setenv(DEFAULT_KEY_ENV, generated)
    assert load_key() == bytes.fromhex(generated)


def test_envelope_bytes_are_json_serializable_metadata(key: bytes) -> None:
    envelope = encrypt_backup(b"data", key, artifact_ref="a")
    json.dumps(envelope.header)


# ---------------------------------------------------------------------------
# Pure policy predicates
# ---------------------------------------------------------------------------


def test_evaluate_restore_passes_on_match_within_rto() -> None:
    result = evaluate_restore(True, 10.0, 60.0)
    assert result.outcome is RestoreOutcome.PASS
    assert result.digest_matched


def test_evaluate_restore_fails_on_digest_mismatch() -> None:
    result = evaluate_restore(False, 10.0, 60.0)
    assert result.outcome is RestoreOutcome.FAIL
    assert not result.digest_matched
    assert "did not match" in result.detail


def test_evaluate_restore_fails_when_rto_is_exceeded() -> None:
    """Byte-correct but too slow is a distinct failure mode."""
    result = evaluate_restore(True, 120.0, 60.0)
    assert result.outcome is RestoreOutcome.FAIL
    assert result.digest_matched
    assert "exceeded RTO" in result.detail


def test_restore_rto_boundary_is_inclusive() -> None:
    assert evaluate_restore(True, 60.0, 60.0).outcome is RestoreOutcome.PASS


def test_freshness_healthy_within_cadence() -> None:
    now = datetime.now(UTC)
    assert check_backup_freshness(now, now - timedelta(hours=2)) is MonitorStatus.HEALTHY


def test_freshness_degraded_inside_warn_window() -> None:
    now = datetime.now(UTC)
    assert check_backup_freshness(now, now - timedelta(hours=26)) is MonitorStatus.DEGRADED


def test_freshness_down_past_warn_window() -> None:
    now = datetime.now(UTC)
    assert check_backup_freshness(now, now - timedelta(hours=72)) is MonitorStatus.DOWN


def test_freshness_down_when_no_backup_exists() -> None:
    assert check_backup_freshness(datetime.now(UTC), None) is MonitorStatus.DOWN


def test_restore_test_due_when_never_run() -> None:
    assert restore_test_due(datetime.now(UTC), None) is True


def test_restore_test_due_respects_the_weekly_cadence() -> None:
    now = datetime.now(UTC)
    assert restore_test_due(now, now - timedelta(days=3)) is False
    assert restore_test_due(now, now - timedelta(days=8)) is True


def test_drill_due_respects_the_monthly_cadence() -> None:
    now = datetime.now(UTC)
    assert evaluate_drill_cadence(now, now - timedelta(days=10)) is False
    assert evaluate_drill_cadence(now, now - timedelta(days=31)) is True


# ---------------------------------------------------------------------------
# Store: artifacts and restore tests
# ---------------------------------------------------------------------------


def _artifact(**overrides: object) -> RecordArtifactRequest:
    payload: dict[str, object] = {
        "artifact_id": "art-1",
        "org_id": "org-1",
        "kind": ArtifactKind.POSTGRES,
        "key_ref": DEFAULT_KEY_ENV,
        "key_fingerprint": "0123456789abcdef",
        "plaintext_sha256": "a" * 64,
        "ciphertext_sha256": "b" * 64,
        "plaintext_bytes": 1024,
        "ciphertext_bytes": 1100,
    }
    payload.update(overrides)
    return RecordArtifactRequest(**payload)  # type: ignore[arg-type]


def test_record_artifact_is_idempotent(store: BackupStore, audit_log: Path) -> None:
    first = store.record_artifact(_artifact())
    second = store.record_artifact(_artifact())
    assert first == second
    assert "backup_artifacts.recorded" in audit_log.read_text(encoding="utf-8")


def test_record_artifact_rejects_field_drift(store: BackupStore) -> None:
    store.record_artifact(_artifact())
    with pytest.raises(ValueError, match="different fields"):
        store.record_artifact(_artifact(plaintext_sha256="c" * 64))


def test_record_artifact_never_stores_a_key(store: BackupStore) -> None:
    """Only an opaque reference and a fingerprint are persisted."""
    stored = store.record_artifact(_artifact(key_ref="CLIPOS_BACKUP_KEY"))
    serialized = json.dumps(stored.model_dump(mode="json"))
    assert "CLIPOS_BACKUP_KEY" in serialized  # the *name*, not a value
    assert stored.key_fingerprint == "0123456789abcdef"


def test_list_artifacts_filters_by_kind(store: BackupStore) -> None:
    store.record_artifact(_artifact())
    store.record_artifact(_artifact(artifact_id="art-2", kind=ArtifactKind.CONFIGURATION))
    assert len(store.list_artifacts("org-1")) == 2
    only_cfg = store.list_artifacts("org-1", ArtifactKind.CONFIGURATION)
    assert [a.artifact_id for a in only_cfg] == ["art-2"]


def test_get_artifact_scopes_by_org(store: BackupStore) -> None:
    store.record_artifact(_artifact())
    assert store.get_artifact("art-1", "org-1") is not None
    assert store.get_artifact("art-1", "org-2") is None


def _restore(**overrides: object) -> RecordRestoreTestRequest:
    payload: dict[str, object] = {
        "test_id": "rt-1",
        "org_id": "org-1",
        "artifact_id": "art-1",
        "environment_ref": "isolated://restore-sandbox-1",
        "digest_matched": True,
        "duration_seconds": 42.0,
        "rpo_seconds": 3600.0,
        "detail": "",
    }
    payload.update(overrides)
    return RecordRestoreTestRequest(**payload)  # type: ignore[arg-type]


def test_record_restore_test_computes_outcome(store: BackupStore, audit_log: Path) -> None:
    store.record_artifact(_artifact())
    stored = store.record_restore_test(_restore())
    assert stored.outcome is RestoreOutcome.PASS
    assert stored.digest_matched is True
    assert stored.environment_ref == "isolated://restore-sandbox-1"
    assert "restore_tests.recorded" in audit_log.read_text(encoding="utf-8")


def test_record_restore_test_fails_on_digest_mismatch(store: BackupStore) -> None:
    store.record_artifact(_artifact())
    stored = store.record_restore_test(_restore(digest_matched=False))
    assert stored.outcome is RestoreOutcome.FAIL


def test_record_restore_test_fails_when_rto_exceeded(store: BackupStore) -> None:
    store.record_artifact(_artifact())
    stored = store.record_restore_test(
        _restore(duration_seconds=99999.0, rto_seconds=DEFAULT_RESTORE_RTO_SECONDS)
    )
    assert stored.outcome is RestoreOutcome.FAIL
    assert "exceeded RTO" in stored.detail


def test_record_restore_test_is_idempotent(store: BackupStore) -> None:
    store.record_artifact(_artifact())
    first = store.record_restore_test(_restore())
    second = store.record_restore_test(_restore())
    assert first.created_at == second.created_at


def test_record_restore_test_rejects_field_drift(store: BackupStore) -> None:
    store.record_artifact(_artifact())
    store.record_restore_test(_restore())
    with pytest.raises(ValueError, match="different fields"):
        store.record_restore_test(_restore(duration_seconds=43.0))


def test_record_restore_test_requires_a_known_artifact(store: BackupStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.record_restore_test(_restore(artifact_id="ghost"))


# ---------------------------------------------------------------------------
# Aggregate status
# ---------------------------------------------------------------------------


def test_status_flags_a_missing_backup(store: BackupStore) -> None:
    status = store.status("org-1")
    assert status.freshness is MonitorStatus.DOWN
    assert status.restore_test_due is True
    assert status.drill_due is True
    assert any("freshness" in item for item in status.attention)


def test_status_healthy_after_a_recent_passing_restore(store: BackupStore) -> None:
    store.record_artifact(_artifact())
    store.record_restore_test(_restore())
    status = store.status("org-1")
    assert status.freshness is MonitorStatus.HEALTHY
    assert status.last_restore_outcome is RestoreOutcome.PASS
    assert status.restore_test_due is False


def test_status_surfaces_a_failed_restore(store: BackupStore) -> None:
    store.record_artifact(_artifact())
    store.record_restore_test(_restore(digest_matched=False))
    status = store.status("org-1")
    assert status.last_restore_outcome is RestoreOutcome.FAIL
    assert any("restore test failed" in item for item in status.attention)


def test_summarize_uses_the_latest_restore_test(store: BackupStore) -> None:
    store.record_artifact(_artifact())
    tests = store.list_restore_tests("org-1")
    assert tests == []
    status = summarize_backup_status("org-1", [], [], datetime.now(UTC))
    assert status.last_restore_outcome is None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _artifact_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_id": "art-1",
        "org_id": "org-1",
        "kind": "POSTGRES",
        "key_ref": DEFAULT_KEY_ENV,
        "key_fingerprint": "0123456789abcdef",
        "plaintext_sha256": "a" * 64,
        "ciphertext_sha256": "b" * 64,
        "plaintext_bytes": 1024,
        "ciphertext_bytes": 1100,
    }
    payload.update(overrides)
    return payload


def _restore_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "test_id": "rt-1",
        "org_id": "org-1",
        "artifact_id": "art-1",
        "environment_ref": "isolated://sandbox-1",
        "digest_matched": True,
        "duration_seconds": 42.0,
        "rpo_seconds": 3600.0,
    }
    payload.update(overrides)
    return payload


def test_route_artifact_roundtrip(client: TestClient) -> None:
    created = client.post("/backups/artifacts", json=_artifact_payload())
    assert created.status_code == 200, created.text
    assert created.json()["kind"] == "POSTGRES"

    fetched = client.get("/backups/artifacts/art-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200

    missing = client.get("/backups/artifacts/ghost", params={"org_id": "org-1"})
    assert missing.status_code == 404


def test_route_restore_test_roundtrip(client: TestClient) -> None:
    client.post("/backups/artifacts", json=_artifact_payload())
    recorded = client.post("/backups/restore-tests", json=_restore_payload())
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["outcome"] == "PASS"

    listed = client.get("/backups/restore-tests", params={"org_id": "org-1"})
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_route_restore_test_requires_artifact(client: TestClient) -> None:
    refused = client.post("/backups/restore-tests", json=_restore_payload())
    assert refused.status_code == 404


def test_route_status_reports_attention(client: TestClient) -> None:
    status = client.get("/backups/status", params={"org_id": "org-1"})
    assert status.status_code == 200
    body = status.json()
    assert body["freshness"] == "DOWN"
    assert body["restore_test_due"] is True


def test_route_list_artifacts_filters_by_kind(client: TestClient) -> None:
    client.post("/backups/artifacts", json=_artifact_payload())
    client.post(
        "/backups/artifacts",
        json=_artifact_payload(artifact_id="art-2", kind="CONFIGURATION"),
    )
    filtered = client.get("/backups/artifacts", params={"org_id": "org-1", "kind": "CONFIGURATION"})
    assert [a["artifact_id"] for a in filtered.json()] == ["art-2"]


def test_routes_are_registered() -> None:
    from fastapi.routing import APIRoute

    from clipos_api.main import app

    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    for expected in (
        "/backups/artifacts",
        "/backups/artifacts/{artifact_id}",
        "/backups/restore-tests",
        "/backups/status",
    ):
        assert expected in paths, f"missing route: {expected}"
