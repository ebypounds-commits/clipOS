"""Tests for the rights ledger (CLIP-031)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit, rights_entities
from clipos_api.main import app
from clipos_api.rights_entities import ContentSource, Creator, SourceItem, SourceType
from clipos_api.rights_ledger import (
    AppliesTo,
    AttributionRequirement,
    Basis,
    EvidenceType,
    License,
    LicenseEvidence,
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
    path = Path("/tmp") / "clipos-031-rights-ledger-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> RightsLedgerStore:
    entities = rights_entities.RightsEntitiesStore.from_engine(engine)
    entities.register_creator(_creator())
    entities.register_source(_source())
    entities.register_item(_item())
    return RightsLedgerStore.from_engine(engine)


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
) -> License:
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


def _evidence(
    evidence_id: str = "ev-1",
    license_id: str = "lic-1",
    **changes: object,
) -> LicenseEvidence:
    values = {
        "evidence_id": evidence_id,
        "license_id": license_id,
        "org_id": "org-1",
        "evidence_type": EvidenceType.CONTRACT,
        "evidence_ref": "blob://evidence/ev-1",
        "captured_at": datetime(2026, 1, 2, tzinfo=UTC),
    }
    values.update(changes)
    return LicenseEvidence.model_validate(values)


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


def _register_record(store: RightsLedgerStore, record: RightsRecord | None = None) -> RightsRecord:
    return store.register_record(record or _record())


def test_record_registration_is_idempotent_and_rejects_conflicts(
    store: RightsLedgerStore,
) -> None:
    registered = _register_record(store)
    assert registered.created_at is not None
    assert _register_record(store) == registered
    with pytest.raises(ValueError, match="different fields"):
        _register_record(store, _record(basis=Basis.OWNED))


def test_record_requires_existing_item_and_matching_org(store: RightsLedgerStore) -> None:
    with pytest.raises(ValueError, match="item not found"):
        _register_record(store, _record(item_id="missing"))
    with pytest.raises(ValueError, match="different organization"):
        _register_record(store, _record(org_id="org-2"))


def test_initial_status_never_auto_clears() -> None:
    with pytest.raises(ValueError, match="UNKNOWN or REVIEW_REQUIRED"):
        _record(status=Status.CLEAR)
    assert {member.value for member in Status} == {
        "UNKNOWN",
        "REVIEW_REQUIRED",
        "CLEAR",
        "RESTRICTED",
        "EXPIRED",
        "REVOKED",
        "REJECTED",
    }
    assert {member.value for member in Basis} == {
        "OWNED",
        "LICENSED",
        "CREATOR_PARTNERSHIP",
        "PUBLIC_DOMAIN",
        "PERMISSIVE_LICENSE",
        "GENERATED",
        "TRANSFORMATIVE_REVIEW",
    }


def test_status_change_appends_event_and_audits(store: RightsLedgerStore, audit_log: Path) -> None:
    _register_record(store)
    event = store.record_status_change("rec-1", "org-1", Status.CLEAR, "refs://decision-1")
    assert event.from_status is Status.REVIEW_REQUIRED
    assert event.to_status is Status.CLEAR
    events = store.record_status_change("rec-1", "org-1", Status.RESTRICTED)
    assert events.to_status is Status.RESTRICTED
    logged = [e for e in audit.read_events() if e["action"] == "rights_ledger.status_changed"]
    assert logged[-1]["details"] == {
        "org_id": "org-1",
        "from_status": "CLEAR",
        "to_status": "RESTRICTED",
        "reason_ref": None,
    }
    assert [e.to_status for e in store.list_events("rec-1", "org-1")] == [
        Status.CLEAR,
        Status.RESTRICTED,
    ]


def test_status_change_rejects_invalid_and_unknown_records(store: RightsLedgerStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.record_status_change("missing", "org-1", Status.CLEAR)
    _register_record(store)
    with pytest.raises(ValueError, match="not found"):
        store.record_status_change("rec-1", "org-2", Status.CLEAR)
    with pytest.raises(ValueError, match="invalid status"):
        store.record_status_change("rec-1", "org-1", "NOT-A-STATUS")  # type: ignore[arg-type]


def test_license_expiry_derivation_with_injectable_clock(store: RightsLedgerStore) -> None:
    _register_record(store)
    store.register_license(_license())
    before = datetime(2026, 12, 31, tzinfo=UTC)
    after = datetime(2027, 1, 2, tzinfo=UTC)
    assert store.effective_status("rec-1", "org-1", now=before) is Status.CLEAR
    assert store.effective_status("rec-1", "org-1", now=after) is Status.EXPIRED
    store.register_license(_license("lic-2", ends_at=None, terms_ref="contracts://lic-2"))
    assert store.effective_status("rec-1", "org-1", now=after) is Status.EXPIRED


def test_license_registration_is_idempotent(store: RightsLedgerStore) -> None:
    _register_record(store)
    registered = store.register_license(_license())
    assert store.register_license(_license()) == registered
    with pytest.raises(ValueError, match="different fields"):
        store.register_license(_license(licensor_ref="secrets://contacts/other"))
    with pytest.raises(ValueError, match="not found"):
        store.register_license(_license(record_id="missing"))


def test_license_requires_matching_org(store: RightsLedgerStore) -> None:
    _register_record(store)
    with pytest.raises(ValueError, match="different organization"):
        store.register_license(_license(org_id="org-2"))


def test_evidence_is_append_only(store: RightsLedgerStore) -> None:
    _register_record(store)
    store.register_license(_license())
    added = store.add_evidence(_evidence())
    assert added.created_at is not None
    with pytest.raises(ValueError, match="append-only"):
        store.add_evidence(_evidence(evidence_ref="blob://evidence/changed"), update=True)
    with pytest.raises(ValueError, match="not found"):
        store.add_evidence(_evidence(license_id="missing"))


def test_evidence_requires_matching_org(store: RightsLedgerStore) -> None:
    _register_record(store)
    store.register_license(_license())
    with pytest.raises(ValueError, match="different organization"):
        store.add_evidence(_evidence(org_id="org-2"))


def test_attribution_requirement_is_idempotent(store: RightsLedgerStore) -> None:
    _register_record(store)
    added = store.add_attribution_requirement(_requirement())
    assert store.add_attribution_requirement(_requirement()) == added
    with pytest.raises(ValueError, match="different fields"):
        store.add_attribution_requirement(_requirement(metadata={"locale": "fr"}))
    with pytest.raises(ValueError, match="not found"):
        store.add_attribution_requirement(_requirement(record_id="missing"))


def test_listings_and_events_are_org_scoped(store: RightsLedgerStore) -> None:
    engine = store._sessions.kw["bind"]
    entities = rights_entities.RightsEntitiesStore.from_engine(engine)
    entities.register_creator(_creator("org-2", "creator-2"))
    entities.register_source(
        _source(source_id="source-2", org_id="org-2").__class__.model_validate(
            {
                **_source(source_id="source-2", org_id="org-2").model_dump(),
                "creator_id": "creator-2",
            }
        )
    )
    entities.register_item(_item("item-2", "org-2", source_id="source-2"))
    _register_record(store)
    store.register_record(_record("rec-2", item_id="item-2", org_id="org-2"))
    assert [r.record_id for r in store.list_records("org-1")] == ["rec-1"]
    assert [r.record_id for r in store.list_records("org-2")] == ["rec-2"]
    store.record_status_change("rec-1", "org-1", Status.CLEAR)
    with pytest.raises(ValueError, match="not found"):
        store.list_events("rec-1", "org-2")
    assert [e.event_id for e in store.list_events("rec-1", "org-1")] == ["evt-rec-1-1"]
    with pytest.raises(ValueError, match="not found"):
        store.effective_status("rec-1", "org-2")


def test_routes_are_wired() -> None:
    names = {route.name for route in app.routes if isinstance(route, APIRoute)}
    assert {
        "register_rights_record",
        "get_rights_record",
        "list_rights_records",
        "change_rights_status",
        "effective_rights_status",
        "register_license",
        "add_license_evidence",
        "add_attribution_requirement",
        "list_rights_events",
    } <= names


def test_no_provider_model_identifiers_or_inline_secrets() -> None:
    src = (
        Path(rights_entities.__file__).read_text()
        + Path(__import__("clipos_api.rights_ledger", fromlist=["x"]).__file__).read_text()
    )
    for token in ("gpt-", "claude-", "gemini-", "deepseek", "qwen", "sk-", "password="):
        assert token not in src, token
