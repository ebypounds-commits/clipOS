"""Tests for authorized media ingestion (CLIP-038)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.media_ingestion import (
    AssetVersion,
    IngestionGate,
    IngestionRequest,
    MediaAsset,
    MediaAssetKind,
    MediaAssetStatus,
    MediaIngestionStore,
    ingest_asset,
)
from clipos_api.rights_ledger import Basis, Status


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.media_ingestion import AssetVersionRecord, MediaAssetRecord  # noqa: F401
    from clipos_api.repository import Base
    from clipos_api.rights_ledger import (  # noqa: F401
        AttributionRequirementRecord,
        LicenseEvidenceRecord,
        LicenseRecord,
        RightsEventRecord,
        RightsRecordRecord,
    )

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-038-ingestion-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> MediaIngestionStore:
    return MediaIngestionStore.from_engine(engine)


@pytest.fixture()
def gate(engine: Engine, audit_log: Path) -> IngestionGate:
    return IngestionGate.from_engine(engine)


def _seed_record(
    store_engine: Engine,
    item_id: str = "src-1",
    org_id: str = "org-1",
    status: Status = Status.CLEAR,
    basis: Basis = Basis.OWNED,
) -> None:
    """Seed a rights record directly so the gate has something to read."""
    from sqlalchemy.orm import sessionmaker

    from clipos_api.rights_ledger import RightsRecordRecord

    sessions = sessionmaker(bind=store_engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            RightsRecordRecord(
                record_id=f"rr-{item_id}",
                item_id=item_id,
                org_id=org_id,
                status=status.value,
                basis=basis.value,
                reviewed_by="seed:test",
                record_metadata={"seed": "test"},
                created_at=datetime.now(UTC),
            )
        )


def _request(**changes: object) -> IngestionRequest:
    values = {
        "media_asset_id": "asset-1",
        "org_id": "org-1",
        "source_item_id": "src-1",
        "kind": MediaAssetKind.VIDEO,
        "title": "Test Asset",
    }
    values.update(changes)
    return IngestionRequest.model_validate(values)


# ----------------------------------------------------------------------
# Gate
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "basis", "allowed"),
    [
        (Status.CLEAR, Basis.OWNED, True),
        (Status.REVIEW_REQUIRED, Basis.OWNED, False),
        (Status.RESTRICTED, Basis.OWNED, False),
        (Status.EXPIRED, Basis.OWNED, False),
        (Status.REVOKED, Basis.OWNED, False),
        (Status.REJECTED, Basis.OWNED, False),
        (Status.UNKNOWN, Basis.OWNED, False),
    ],
)
def test_gate_status_matrix(
    gate: IngestionGate,
    engine: Engine,
    status: Status,
    basis: Basis,
    allowed: bool,
) -> None:
    _seed_record(engine, status=status, basis=basis)
    decision = gate.authorize("src-1", "org-1")
    assert decision.allowed is allowed


def test_gate_generated_basis_allowed_even_if_not_clear(
    gate: IngestionGate, engine: Engine
) -> None:
    _seed_record(engine, status=Status.REVIEW_REQUIRED, basis=Basis.GENERATED)
    decision = gate.authorize("src-1", "org-1")
    assert decision.allowed is True


def test_gate_no_record_denied(gate: IngestionGate) -> None:
    decision = gate.authorize("missing", "org-1")
    assert decision.allowed is False
    assert "no rights record" in decision.reason


def test_gate_org_isolated(gate: IngestionGate, engine: Engine) -> None:
    _seed_record(engine, org_id="org-1")
    assert gate.authorize("src-1", "org-1").allowed is True
    assert gate.authorize("src-1", "org-2").allowed is False


# ----------------------------------------------------------------------
# Ingestion flow
# ----------------------------------------------------------------------


def test_ingest_allowed_creates_asset(
    gate: IngestionGate, store: MediaIngestionStore, engine: Engine
) -> None:
    _seed_record(engine)
    decision, asset = ingest_asset(gate, store, _request())
    assert decision.allowed is True
    assert asset.status is MediaAssetStatus.PENDING
    assert asset.source_item_id == "src-1"


def test_ingest_denied_raises_permission(gate: IngestionGate, store: MediaIngestionStore) -> None:
    with pytest.raises(PermissionError, match="no rights record"):
        ingest_asset(gate, store, _request())


def test_ingest_idempotent(gate: IngestionGate, store: MediaIngestionStore, engine: Engine) -> None:
    _seed_record(engine)
    _d1, first = ingest_asset(gate, store, _request())
    _d2, again = ingest_asset(gate, store, _request())
    assert first == again


def test_ingest_conflicting_fields_raise(
    gate: IngestionGate, store: MediaIngestionStore, engine: Engine
) -> None:
    _seed_record(engine)
    ingest_asset(gate, store, _request())
    with pytest.raises(ValueError, match="different fields"):
        ingest_asset(gate, store, _request(title="Different"))


# ----------------------------------------------------------------------
# Assets + versions
# ----------------------------------------------------------------------


def test_asset_org_isolated(store: MediaIngestionStore) -> None:
    store.register_asset(
        MediaAsset(media_asset_id="asset-1", org_id="org-1", kind=MediaAssetKind.VIDEO, title="A")
    )
    store.register_asset(
        MediaAsset(media_asset_id="asset-1", org_id="org-2", kind=MediaAssetKind.IMAGE, title="B")
    )
    assert len(store.list_assets("org-1")) == 1
    assert len(store.list_assets("org-2")) == 1


def test_version_requires_existing_asset(store: MediaIngestionStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.record_version(
            AssetVersion(
                media_asset_id="missing",
                org_id="org-1",
                version=1,
                storage_ref="s3://bucket/a.mp4",
                bytes=100,
                checksum_sha256="a" * 64,
            )
        )


def test_versions_monotonic_and_conflict(store: MediaIngestionStore) -> None:
    store.register_asset(
        MediaAsset(media_asset_id="asset-1", org_id="org-1", kind=MediaAssetKind.VIDEO, title="A")
    )
    v1 = store.record_version(
        AssetVersion(
            media_asset_id="asset-1",
            org_id="org-1",
            version=1,
            storage_ref="s3://bucket/a.mp4",
            bytes=100,
            checksum_sha256="a" * 64,
        )
    )
    assert v1.version == 1
    # Skip version 2 -> reject.
    with pytest.raises(ValueError, match="max\\+1"):
        store.record_version(
            AssetVersion(
                media_asset_id="asset-1",
                org_id="org-1",
                version=3,
                storage_ref="s3://bucket/a3.mp4",
                bytes=300,
                checksum_sha256="c" * 64,
            )
        )
    v2 = store.record_version(
        AssetVersion(
            media_asset_id="asset-1",
            org_id="org-1",
            version=2,
            storage_ref="s3://bucket/a2.mp4",
            bytes=200,
            checksum_sha256="b" * 64,
        )
    )
    assert v2.version == 2
    listed = store.list_versions("asset-1")
    assert [v.version for v in listed] == [1, 2]


# ----------------------------------------------------------------------
# Audit + routes
# ----------------------------------------------------------------------


def test_ingest_writes_audit(
    gate: IngestionGate, store: MediaIngestionStore, engine: Engine
) -> None:
    _seed_record(engine)
    ingest_asset(gate, store, _request())
    actions = [e["action"] for e in audit.read_events()]
    assert "media_ingestion.asset_registered" in actions
    assert "media_ingestion.gate_allowed" in actions


def test_route_denied_maps_to_403(gate: IngestionGate, store: MediaIngestionStore) -> None:
    from clipos_api.media_ingestion import ingest_media_asset_route

    with pytest.raises(HTTPException) as err:
        ingest_media_asset_route(_request(), gate, store)
    assert err.value.status_code == 403


def test_route_asset_missing_maps_to_404(store: MediaIngestionStore) -> None:
    from clipos_api.media_ingestion import get_media_asset_route

    with pytest.raises(HTTPException) as err:
        get_media_asset_route("missing", store, "org-1")
    assert err.value.status_code == 404


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/media-assets" in paths
    assert "/media-assets/{media_asset_id}" in paths
    assert "/media-assets/{media_asset_id}/versions" in paths
    assert "/media-ingestion/authorize" in paths
