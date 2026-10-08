"""Tests for the deterministic rights publication gate (CLIP-033)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit, rights_entities
from clipos_api.main import app
from clipos_api.provenance import (
    ProvenanceManifest,
    ProvenanceStep,
    ProvenanceStore,
    StepType,
)
from clipos_api.rights_entities import ContentSource, Creator, SourceItem, SourceType
from clipos_api.rights_gate import (
    RightsGateDecision,
    RightsGateReason,
    RightsPublicationGate,
    router,
)
from clipos_api.rights_ledger import (
    AppliesTo,
    AttributionRequirement,
    Basis,
    RightsLedgerStore,
    RightsRecord,
    Status,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    rights_entities.Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-033-rights-gate-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


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


def _record(record_id: str = "rec-1", item_id: str = "item-1", **changes: object) -> RightsRecord:
    values = {
        "record_id": record_id,
        "item_id": item_id,
        "org_id": "org-1",
        "status": Status.REVIEW_REQUIRED,
        "basis": Basis.LICENSED,
        "reviewed_by": "reviewer:ada",
        "metadata": {"source": "contract"},
    }
    values.update(changes)
    return RightsRecord.model_validate(values)


def _license(
    license_id: str = "lic-1",
    record_id: str = "rec-1",
    **changes: object,
) -> object:
    from clipos_api.rights_ledger import License

    values = {
        "license_id": license_id,
        "record_id": record_id,
        "org_id": "org-1",
        "licensor_ref": "secrets://contacts/licensor-1",
        "licensee_ref": "secrets://contacts/licensee-1",
        "starts_at": datetime(2026, 1, 1, tzinfo=UTC),
        "ends_at": datetime(2027, 1, 1, tzinfo=UTC),
        "terms_ref": "contracts://lic-1",
    }
    values.update(changes)
    return License.model_validate(values)


def _requirement(
    requirement_id: str = "req-1",
    record_id: str = "rec-1",
    **changes: object,
) -> AttributionRequirement:
    values = {
        "requirement_id": requirement_id,
        "record_id": record_id,
        "org_id": "org-1",
        "requirement": 'Credit line: "© Ada Lovelace"',
        "applies_to": AppliesTo.PUBLICATION,
        "metadata": {"locale": "en"},
    }
    values.update(changes)
    return AttributionRequirement.model_validate(values)


def _entities_store(engine: Engine) -> rights_entities.RightsEntitiesStore:
    store = rights_entities.RightsEntitiesStore.from_engine(engine)
    store.register_creator(_creator())
    store.register_source(_source())
    store.register_item(_item())
    return store


def _ledger(engine: Engine) -> RightsLedgerStore:
    return RightsLedgerStore.from_engine(engine)


def _provenance(engine: Engine) -> ProvenanceStore:
    return ProvenanceStore.from_engine(engine)


def _add_manifest(
    prov: ProvenanceStore, item_id: str = "item-1", org_id: str = "org-1"
) -> ProvenanceManifest:
    step = ProvenanceStep.model_validate(
        {
            "step_type": StepType.TRANSFORMED,
            "actor_ref": "operator:orchestrator",
            "at": datetime(2026, 1, 2, tzinfo=UTC),
            "inputs_refs": [f"storage://source-items/{item_id}"],
            "outputs_ref": f"storage://renders/{item_id}",
            "transform_note": "render step",
        }
    )
    return prov.append_step(item_id, org_id, step)


def _gate(engine: Engine, provenance_enabled: bool = True) -> RightsPublicationGate:
    prov = _provenance(engine) if provenance_enabled else None
    return RightsPublicationGate(_ledger(engine), prov)


def _evaluate(
    gate: RightsPublicationGate,
    item_id: str = "item-1",
    org_id: str = "org-1",
    checked_at: datetime | None = None,
) -> RightsGateDecision:
    return gate.evaluate(item_id, org_id, now=checked_at, checked_at=checked_at)


def test_decision_is_deterministic_and_injectable_clock(engine: Engine, audit_log: Path) -> None:
    _entities_store(engine)
    ledger = _ledger(engine)
    ledger.register_record(_record())
    gate = _gate(engine, provenance_enabled=False)

    at = datetime(2026, 6, 1, tzinfo=UTC)
    first = _evaluate(gate, checked_at=at)
    second = _evaluate(gate, checked_at=at)
    assert first == second
    assert first.checked_at == at
    assert first.allowed is False
    assert first.reason is RightsGateReason.STATUS_NOT_CLEAR
    assert first.record_status is Status.REVIEW_REQUIRED


def test_clear_status_allows_publication(engine: Engine, audit_log: Path) -> None:
    _entities_store(engine)
    ledger = _ledger(engine)
    ledger.register_record(_record(status=Status.UNKNOWN))
    ledger.record_status_change("rec-1", "org-1", Status.CLEAR, "reviewer:ada")
    gate = _gate(engine, provenance_enabled=False)

    decision = _evaluate(gate)
    assert decision.allowed is True
    assert decision.reason is RightsGateReason.CLEAR
    assert decision.record_status is Status.CLEAR


def test_not_found_denies(engine: Engine, audit_log: Path) -> None:
    _entities_store(engine)
    gate = _gate(engine, provenance_enabled=False)

    decision = _evaluate(gate, item_id="missing")
    assert decision.allowed is False
    assert decision.reason is RightsGateReason.NOT_FOUND
    assert decision.record_status is Status.UNKNOWN


def test_expired_license_denies(engine: Engine, audit_log: Path) -> None:
    _entities_store(engine)
    ledger = _ledger(engine)
    ledger.register_record(_record())
    ledger.register_license(_license())
    gate = _gate(engine, provenance_enabled=False)

    decision = _evaluate(gate, checked_at=datetime(2027, 6, 1, tzinfo=UTC))
    assert decision.allowed is False
    assert decision.reason is RightsGateReason.EXPIRED
    assert decision.record_status is Status.EXPIRED


def test_active_license_backs_clear(engine: Engine, audit_log: Path) -> None:
    _entities_store(engine)
    ledger = _ledger(engine)
    ledger.register_record(_record(status=Status.UNKNOWN))
    ledger.register_license(_license())
    gate = _gate(engine, provenance_enabled=False)

    decision = _evaluate(gate, checked_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert decision.allowed is True
    assert decision.reason is RightsGateReason.CLEAR
    assert decision.record_status is Status.CLEAR


def test_missing_attribution_denies_clear_record(engine: Engine, audit_log: Path) -> None:
    _entities_store(engine)
    ledger = _ledger(engine)
    ledger.register_record(_record(status=Status.UNKNOWN))
    ledger.record_status_change("rec-1", "org-1", Status.CLEAR, "reviewer:ada")
    ledger.add_attribution_requirement(_requirement())
    gate = _gate(engine, provenance_enabled=False)

    decision = _evaluate(gate)
    assert decision.allowed is False
    assert decision.reason is RightsGateReason.MISSING_ATTRIBUTION


def test_satisfied_attribution_allows(engine: Engine, audit_log: Path) -> None:
    _entities_store(engine)
    ledger = _ledger(engine)
    ledger.register_record(_record(status=Status.UNKNOWN, metadata={"attribution": "satisfied"}))
    ledger.record_status_change("rec-1", "org-1", Status.CLEAR, "reviewer:ada")
    ledger.add_attribution_requirement(_requirement())
    gate = _gate(engine, provenance_enabled=False)

    decision = _evaluate(gate)
    assert decision.allowed is True
    assert decision.reason is RightsGateReason.CLEAR


def test_missing_provenance_manifest_denies(engine: Engine, audit_log: Path) -> None:
    _entities_store(engine)
    ledger = _ledger(engine)
    ledger.register_record(_record(status=Status.UNKNOWN))
    ledger.record_status_change("rec-1", "org-1", Status.CLEAR, "reviewer:ada")
    gate = _gate(engine, provenance_enabled=True)

    decision = _evaluate(gate)
    assert decision.allowed is False
    assert decision.reason is RightsGateReason.MISSING_PROVENANCE


def test_present_provenance_manifest_ok(engine: Engine, audit_log: Path) -> None:
    _entities_store(engine)
    ledger = _ledger(engine)
    ledger.register_record(_record(status=Status.UNKNOWN))
    ledger.record_status_change("rec-1", "org-1", Status.CLEAR, "reviewer:ada")
    prov = _provenance(engine)
    _add_manifest(prov)
    gate = RightsPublicationGate(ledger, prov)

    decision = _evaluate(gate)
    assert decision.allowed is True
    assert decision.reason is RightsGateReason.CLEAR


def test_restrevoked_denies(engine: Engine, audit_log: Path) -> None:
    _entities_store(engine)
    ledger = _ledger(engine)
    ledger.register_record(_record(status=Status.UNKNOWN))
    ledger.record_status_change("rec-1", "org-1", Status.REVOKED, "reviewer:ada")
    gate = _gate(engine, provenance_enabled=False)

    decision = _evaluate(gate)
    assert decision.allowed is False
    assert decision.reason is RightsGateReason.STATUS_NOT_CLEAR
    assert decision.record_status is Status.REVOKED


def test_gate_writes_audit_events(engine: Engine, audit_log: Path) -> None:
    _entities_store(engine)
    ledger = _ledger(engine)
    ledger.register_record(_record())
    gate = _gate(engine, provenance_enabled=False)
    _evaluate(gate)

    events = audit.read_events()
    assert any(e["action"] == "rights_gate.evaluate" for e in events)


def test_route_registered() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert any("/rights-gate/{item_id}/evaluate" in p for p in paths)


def test_router_meta() -> None:
    assert router.tags == ["rights-gate"]
