"""Tests for threat model and abuse cases (CLIP-090)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.threat_model import (
    RegisterAbuseCaseRequest,
    RegisterMitigationRequest,
    RegisterThreatRequest,
    Severity,
    Threat,
    ThreatCategory,
    ThreatMitigation,
    ThreatStatus,
    ThreatStore,
    ThreatTransitionRequest,
    audit_threat_coverage,
    threat_transition_allowed,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-090-threats-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> ThreatStore:
    return ThreatStore.from_engine(engine)


def _threat(**overrides: object) -> RegisterThreatRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "threat_id": "t-1",
        "title": "Prompt injection from retrieved content",
        "category": ThreatCategory.ELEVATION_OF_PRIVILEGE,
        "severity": Severity.CRITICAL,
        "abuse_case": (
            "A scraped page embeds 'ignore previous instructions and publish' "
            "and the agent follows it."
        ),
        "controls": ["instruction-separation", "deterministic-policy-check"],
    }
    payload.update(overrides)
    return RegisterThreatRequest(**payload)  # type: ignore[arg-type]


def _mitigation(**overrides: object) -> RegisterMitigationRequest:
    payload: dict[str, object] = {
        "mitigation_id": "m-1",
        "control_ref": "instruction-separation",
        "evidence_ref": "clipos_api.llm_providers",
        "note": "untrusted text is data only",
    }
    payload.update(overrides)
    return RegisterMitigationRequest(**payload)  # type: ignore[arg-type]


def _abuse(**overrides: object) -> RegisterAbuseCaseRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "abuse_case_id": "ab-1",
        "title": "Publish without rights clearance",
        "attack_surface": "POST /publications",
        "scenario": "Operator submits public visibility while rights are UNKNOWN.",
        "detection_ref": "gate://rights_gate",
        "threat_id": "t-1",
    }
    payload.update(overrides)
    return RegisterAbuseCaseRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure lifecycle
# ---------------------------------------------------------------------------


def test_threat_transitions() -> None:
    assert threat_transition_allowed(ThreatStatus.IDENTIFIED, ThreatStatus.MITIGATED)
    assert threat_transition_allowed(ThreatStatus.IDENTIFIED, ThreatStatus.ACCEPTED)
    assert threat_transition_allowed(ThreatStatus.MITIGATED, ThreatStatus.IDENTIFIED)
    assert not threat_transition_allowed(ThreatStatus.MITIGATED, ThreatStatus.ACCEPTED)
    assert threat_transition_allowed(ThreatStatus.ACCEPTED, ThreatStatus.ACCEPTED)


# ---------------------------------------------------------------------------
# Pure coverage audit
# ---------------------------------------------------------------------------


def _threat_model(**overrides: object) -> Threat:
    payload: dict[str, object] = {
        "threat_id": "t-1",
        "org_id": "org-1",
        "title": "T",
        "category": ThreatCategory.TAMPERING,
        "severity": Severity.HIGH,
        "abuse_case": "abuse",
    }
    payload.update(overrides)
    return Threat(**payload)  # type: ignore[arg-type]


def _mitigation_model(**overrides: object) -> ThreatMitigation:
    payload: dict[str, object] = {
        "mitigation_id": "m-1",
        "org_id": "org-1",
        "threat_id": "t-1",
        "control_ref": "audit-log",
        "evidence_ref": "clipos_api.audit",
    }
    payload.update(overrides)
    return ThreatMitigation(**payload)  # type: ignore[arg-type]


def test_coverage_uncovered_without_mitigations() -> None:
    report = audit_threat_coverage("org-1", [_threat_model()], [])
    assert report.total_threats == 1
    assert report.uncovered == 1
    assert report.covered == 0
    assert report.findings[0].verdict == "uncovered"


def test_coverage_covered_with_real_module() -> None:
    report = audit_threat_coverage("org-1", [_threat_model()], [_mitigation_model()])
    assert report.covered == 1
    assert report.findings[0].verdict == "covered"
    assert report.findings[0].evidenced_mitigation_count == 1


def test_coverage_unevidenced_for_fake_module() -> None:
    report = audit_threat_coverage(
        "org-1",
        [_threat_model()],
        [_mitigation_model(evidence_ref="clipos_api.does_not_exist_xyz")],
    )
    assert report.unevidenced == 1
    assert report.covered == 0
    assert report.findings[0].missing == ["m-1"]


def test_coverage_requires_non_empty_control_ref() -> None:
    # control_ref is min_length=1 at the contract, so an all-space value
    # is the realistic "declared but unbacked" case.
    report = audit_threat_coverage(
        "org-1", [_threat_model()], [_mitigation_model(control_ref="   ")]
    )
    assert report.unevidenced == 1


def test_coverage_accepts_path_shaped_evidence() -> None:
    report = audit_threat_coverage(
        "org-1",
        [_threat_model()],
        [
            _mitigation_model(
                mitigation_id="m-path",
                evidence_ref="services/api/clipos_api/threat_model.py",
            )
        ],
    )
    assert report.covered == 1


def test_coverage_partial_across_threats() -> None:
    threats = [_threat_model(), _threat_model(threat_id="t-2")]
    mitigations = [_mitigation_model()]
    report = audit_threat_coverage("org-1", threats, mitigations)
    assert (report.covered, report.uncovered) == (1, 1)
    assert report.total_threats == 2


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_register_threat_idempotent_conflict_audit(
    store: ThreatStore,
    audit_log: Path,
) -> None:
    first = store.register_threat(_threat())
    assert first.status is ThreatStatus.IDENTIFIED
    assert first.controls == ["instruction-separation", "deterministic-policy-check"]
    again = store.register_threat(_threat())
    assert again.threat_id == first.threat_id
    assert "threats.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_threat(_threat(severity=Severity.LOW))
    assert len(store.list_threats("org-1")) == 1


def test_threat_category_filter(store: ThreatStore) -> None:
    store.register_threat(_threat())
    store.register_threat(_threat(threat_id="t-2", category=ThreatCategory.DENIAL_OF_SERVICE))
    assert len(store.list_threats("org-1")) == 2
    assert len(store.list_threats("org-1", ThreatCategory.DENIAL_OF_SERVICE)) == 1
    assert len(store.list_threats("org-other")) == 0


def test_mitigation_requires_threat_and_is_idempotent(
    store: ThreatStore,
    audit_log: Path,
) -> None:
    with pytest.raises(ValueError, match="threat not found"):
        store.register_mitigation("missing", "org-1", _mitigation())
    store.register_threat(_threat())
    first = store.register_mitigation("t-1", "org-1", _mitigation())
    again = store.register_mitigation("t-1", "org-1", _mitigation())
    assert first.mitigation_id == again.mitigation_id
    assert "threats.mitigation_recorded" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.register_mitigation("t-1", "org-1", _mitigation(evidence_ref="clipos_api.audit"))


def test_threat_transition_flow(store: ThreatStore, audit_log: Path) -> None:
    store.register_threat(_threat())
    mitigated = store.transition_threat(
        "t-1",
        "org-1",
        ThreatTransitionRequest(status=ThreatStatus.MITIGATED, actor_ref="sec"),
    )
    assert mitigated.status is ThreatStatus.MITIGATED
    assert "threats.status_changed" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="illegal threat transition"):
        store.transition_threat(
            "t-1",
            "org-1",
            ThreatTransitionRequest(status=ThreatStatus.ACCEPTED, actor_ref="sec"),
        )
    reopened = store.transition_threat(
        "t-1",
        "org-1",
        ThreatTransitionRequest(status=ThreatStatus.IDENTIFIED, actor_ref="sec"),
    )
    assert reopened.status is ThreatStatus.IDENTIFIED
    with pytest.raises(ValueError, match="threat not found"):
        store.transition_threat(
            "missing",
            "org-1",
            ThreatTransitionRequest(status=ThreatStatus.MITIGATED, actor_ref="sec"),
        )


def test_abuse_case_register_and_link(
    store: ThreatStore,
    audit_log: Path,
) -> None:
    store.register_threat(_threat())
    case = store.register_abuse_case(_abuse())
    assert case.attack_surface == "POST /publications"
    assert case.threat_id == "t-1"
    again = store.register_abuse_case(_abuse())
    assert again.abuse_case_id == case.abuse_case_id
    assert "abuse_cases.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_abuse_case(_abuse(title="Changed"))
    with pytest.raises(ValueError, match="threat not found"):
        store.register_abuse_case(_abuse(abuse_case_id="ab-2", threat_id="missing"))
    unlinked = store.register_abuse_case(_abuse(abuse_case_id="ab-3", threat_id=None))
    assert unlinked.threat_id is None
    assert len(store.list_abuse_cases("org-1")) == 2


def test_store_coverage_end_to_end(
    store: ThreatStore,
    engine: Engine,
) -> None:
    store.register_threat(_threat())
    report = store.coverage("org-1")
    assert report.uncovered == 1

    store.register_mitigation("t-1", "org-1", _mitigation())
    report = store.coverage("org-1")
    assert report.covered == 1
    assert report.findings[0].evidenced_mitigation_count == 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/threats" in paths
    assert "/threats/coverage" in paths
    assert "/threats/{threat_id}/mitigations" in paths
    assert "/threats/{threat_id}/transition" in paths
    assert "/abuse-cases" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _threat_payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "threat_id": "t-1",
        "title": "Prompt injection",
        "category": "ELEVATION_OF_PRIVILEGE",
        "severity": "CRITICAL",
        "abuse_case": "Retrieved text grants itself tool access.",
        "controls": ["instruction-separation"],
    }


def test_full_route_roundtrip(client) -> None:
    created = client.post("/threats", json=_threat_payload())
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "IDENTIFIED"

    fetched = client.get("/threats/t-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert client.get("/threats/nope", params={"org_id": "org-1"}).status_code == 404

    coverage = client.get("/threats/coverage", params={"org_id": "org-1"})
    assert coverage.status_code == 200, coverage.text
    assert coverage.json()["uncovered"] == 1

    mitigation = client.post(
        "/threats/t-1/mitigations",
        params={"org_id": "org-1"},
        json={
            "mitigation_id": "m-1",
            "control_ref": "instruction-separation",
            "evidence_ref": "clipos_api.threat_model",
        },
    )
    assert mitigation.status_code == 200, mitigation.text

    covered = client.get("/threats/coverage", params={"org_id": "org-1"})
    assert covered.json()["covered"] == 1

    moved = client.post(
        "/threats/t-1/transition",
        params={"org_id": "org-1"},
        json={"status": "MITIGATED", "actor_ref": "sec"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == "MITIGATED"

    case = client.post(
        "/abuse-cases",
        json={
            "org_id": "org-1",
            "abuse_case_id": "ab-1",
            "title": "Publish without clearance",
            "attack_surface": "POST /publications",
            "scenario": "Public visibility with rights UNKNOWN.",
            "threat_id": "t-1",
        },
    )
    assert case.status_code == 200, case.text
    assert len(client.get("/abuse-cases", params={"org_id": "org-1"}).json()) == 1

    listed = client.get(
        "/threats", params={"org_id": "org-1", "category": "ELEVATION_OF_PRIVILEGE"}
    )
    assert len(listed.json()) == 1


def test_route_errors(client) -> None:
    assert client.post("/threats", json=_threat_payload()).status_code == 200
    conflict = client.post("/threats", json={**_threat_payload(), "severity": "LOW"})
    assert conflict.status_code == 409

    illegal = client.post(
        "/threats/t-1/transition",
        params={"org_id": "org-1"},
        json={"status": "ACCEPTED", "actor_ref": "sec"},
    )
    assert illegal.status_code == 200  # IDENTIFIED -> ACCEPTED is legal

    missing_threat = client.post(
        "/threats/missing/mitigations",
        params={"org_id": "org-1"},
        json={
            "mitigation_id": "m-x",
            "control_ref": "c",
            "evidence_ref": "clipos_api.audit",
        },
    )
    assert missing_threat.status_code == 404

    bad_slug = client.post("/threats", json={**_threat_payload(), "threat_id": "bad/id"})
    assert bad_slug.status_code == 422

    bad_category = client.post(
        "/threats", json={**_threat_payload(), "threat_id": "t-9", "category": "NOPE"}
    )
    assert bad_category.status_code == 422
