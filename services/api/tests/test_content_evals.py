"""Tests for factuality evaluations and transformation records (CLIP-049)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.content_evals import (
    ContentEvalStore,
    EvalResult,
    FactualityEvaluation,
    TransformationRecord,
)
from clipos_api.content_planning import ContentBrief, ContentPlanningStore
from clipos_api.main import app
from clipos_api.research import (
    Claim,
    ClaimVeracity,
    ResearchPacket,
    ResearchStore,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-049-content-evals-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ContentEvalStore:
    return ContentEvalStore.from_engine(engine)


def _seed_claim(engine: Engine, claim_id: str = "claim-1") -> None:
    research = ResearchStore.from_engine(engine)
    research.register_packet(
        ResearchPacket(research_packet_id="packet-1", org_id="org-1", title="Fixture")
    )
    research.register_claim(
        Claim(
            claim_id=claim_id,
            org_id="org-1",
            research_packet_id="packet-1",
            statement="A claim to evaluate",
        )
    )


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


def _evaluation(
    eval_id: str = "eval-1",
    result: EvalResult = EvalResult.PASS,
    claim_id: str = "claim-1",
    **overrides: object,
) -> FactualityEvaluation:
    payload: dict[str, object] = {
        "eval_id": eval_id,
        "org_id": "org-1",
        "claim_id": claim_id,
        "result": result,
        "method_ref": "manual-review",
        "actor_ref": "reviewer-1",
    }
    payload.update(overrides)
    return FactualityEvaluation(**payload)  # type: ignore[arg-type]


def _claim_veracity(engine: Engine, claim_id: str = "claim-1") -> ClaimVeracity:
    research = ResearchStore.from_engine(engine)
    claim = research.get_claim(claim_id, "org-1")
    assert claim is not None
    return claim.veracity


# ---------------------------------------------------------------------------
# Factuality evaluations
# ---------------------------------------------------------------------------


def test_record_evaluation_requires_existing_claim(store: ContentEvalStore) -> None:
    with pytest.raises(ValueError, match="claim not found"):
        store.record_evaluation(_evaluation(claim_id="missing"))


def test_pass_maps_claim_to_verified(
    store: ContentEvalStore, engine: Engine, audit_log: Path
) -> None:
    _seed_claim(engine)
    recorded = store.record_evaluation(_evaluation(result=EvalResult.PASS))
    assert recorded.result is EvalResult.PASS
    assert _claim_veracity(engine) is ClaimVeracity.VERIFIED
    log = audit_log.read_text(encoding="utf-8")
    assert "factuality_evaluations.recorded" in log
    assert "claims.veracity_changed" in log
    assert "eval-1" in log  # reason_ref points at the evaluation


def test_fail_maps_claim_to_disputed(store: ContentEvalStore, engine: Engine) -> None:
    _seed_claim(engine)
    store.record_evaluation(_evaluation(result=EvalResult.FAIL))
    assert _claim_veracity(engine) is ClaimVeracity.DISPUTED


def test_inconclusive_leaves_veracity_unchanged(
    store: ContentEvalStore, engine: Engine, audit_log: Path
) -> None:
    _seed_claim(engine)
    store.record_evaluation(_evaluation(result=EvalResult.INCONCLUSIVE))
    assert _claim_veracity(engine) is ClaimVeracity.UNVERIFIED
    log = audit_log.read_text(encoding="utf-8")
    assert "claims.veracity_changed" not in log


def test_evaluation_idempotent_identical(store: ContentEvalStore, engine: Engine) -> None:
    _seed_claim(engine)
    first = store.record_evaluation(_evaluation())
    second = store.record_evaluation(_evaluation())
    assert first.eval_id == second.eval_id
    assert len(store.list_evaluations("org-1", "claim-1")) == 1


def test_evaluation_conflict_on_different_fields(store: ContentEvalStore, engine: Engine) -> None:
    _seed_claim(engine)
    store.record_evaluation(_evaluation())
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.record_evaluation(_evaluation(result=EvalResult.FAIL))


def test_list_evaluations_missing_claim_raises(store: ContentEvalStore) -> None:
    with pytest.raises(ValueError, match="claim not found"):
        store.list_evaluations("org-1", "missing")


def test_transition_skipped_when_veracity_already_matches(
    store: ContentEvalStore, engine: Engine, audit_log: Path
) -> None:
    _seed_claim(engine)
    store.record_evaluation(_evaluation(eval_id="first", result=EvalResult.PASS))
    log_before = audit_log.read_text(encoding="utf-8").count("claims.veracity_changed")
    store.record_evaluation(_evaluation(eval_id="second", result=EvalResult.PASS))
    log_after = audit_log.read_text(encoding="utf-8").count("claims.veracity_changed")
    assert log_after == log_before  # already VERIFIED -> no second transition


# ---------------------------------------------------------------------------
# Transformation records
# ---------------------------------------------------------------------------


def test_register_transformation_requires_existing_brief(
    store: ContentEvalStore,
) -> None:
    with pytest.raises(ValueError, match="content brief not found"):
        store.register_transformation(
            TransformationRecord(
                transformation_id="t-1",
                org_id="org-1",
                brief_id="missing",
                from_ref="src:original",
                to_ref="out:cut",
                method="render-cut",
            )
        )


def test_register_transformation_idempotent(store: ContentEvalStore, engine: Engine) -> None:
    _seed_brief(engine)
    record = TransformationRecord(
        transformation_id="t-1",
        org_id="org-1",
        brief_id="brief-1",
        from_ref="src:original",
        to_ref="out:cut",
        method="render-cut",
        description="First pass cut",
    )
    first = store.register_transformation(record)
    second = store.register_transformation(record)
    assert first.transformation_id == second.transformation_id
    assert len(store.list_transformations("org-1", "brief-1")) == 1


def test_transformation_conflict_on_different_fields(
    store: ContentEvalStore, engine: Engine
) -> None:
    _seed_brief(engine)
    record = TransformationRecord(
        transformation_id="t-1",
        org_id="org-1",
        brief_id="brief-1",
        from_ref="src:original",
        to_ref="out:cut",
        method="render-cut",
    )
    store.register_transformation(record)
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.register_transformation(
            TransformationRecord(
                transformation_id="t-1",
                org_id="org-1",
                brief_id="brief-1",
                from_ref="src:changed",
                to_ref="out:cut",
                method="render-cut",
            )
        )


def test_transformation_audit_recorded(
    store: ContentEvalStore, engine: Engine, audit_log: Path
) -> None:
    _seed_brief(engine)
    store.register_transformation(
        TransformationRecord(
            transformation_id="t-1",
            org_id="org-1",
            brief_id="brief-1",
            from_ref="src:original",
            to_ref="out:cut",
            method="render-cut",
        )
    )
    assert "transformation_records.registered" in audit_log.read_text(encoding="utf-8")


def test_list_transformations_missing_brief_raises(store: ContentEvalStore) -> None:
    with pytest.raises(ValueError, match="content brief not found"):
        store.list_transformations("org-1", "missing")


def test_list_transformations_org_scoped(store: ContentEvalStore, engine: Engine) -> None:
    _seed_brief(engine)
    store.register_transformation(
        TransformationRecord(
            transformation_id="t-1",
            org_id="org-1",
            brief_id="brief-1",
            from_ref="src:original",
            to_ref="out:cut",
            method="render-cut",
        )
    )
    assert len(store.list_transformations("org-1", "brief-1")) == 1
    # brief is org-scoped: a foreign org cannot see it (404, not empty list)
    with pytest.raises(ValueError, match="content brief not found"):
        store.list_transformations("org-other", "brief-1")


# ---------------------------------------------------------------------------
# Routes (real requests — PEP 563 probe)
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/factuality-evaluations" in paths
    assert "/transformation-records" in paths


def test_router_meta() -> None:
    from clipos_api.content_evals import router

    assert router.tags == ["content-evals"]


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(app)


def _seed_claim_http(client, claim_id: str = "claim-1") -> None:
    assert (
        client.post(
            "/research-packets",
            json={"research_packet_id": "packet-1", "org_id": "org-1", "title": "Fixture"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/research-packets/packet-1/claims",
            json={
                "claim_id": claim_id,
                "org_id": "org-1",
                "research_packet_id": "packet-1",
                "statement": "A claim to evaluate",
            },
        ).status_code
        == 200
    )


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


def test_route_evaluation_roundtrip_reflects_claim_veracity(client) -> None:
    _seed_claim_http(client)
    response = client.post(
        "/factuality-evaluations",
        json={
            "eval_id": "eval-1",
            "org_id": "org-1",
            "claim_id": "claim-1",
            "result": "PASS",
            "method_ref": "manual-review",
            "actor_ref": "reviewer-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["result"] == "PASS"

    claims = client.get("/research-packets/packet-1/claims", params={"org_id": "org-1"})
    assert claims.json()[0]["veracity"] == "VERIFIED"

    listing = client.get(
        "/factuality-evaluations", params={"org_id": "org-1", "claim_id": "claim-1"}
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 1


def test_route_transformation_roundtrip(client) -> None:
    _seed_brief_http(client)
    response = client.post(
        "/transformation-records",
        json={
            "transformation_id": "t-1",
            "org_id": "org-1",
            "brief_id": "brief-1",
            "from_ref": "src:original",
            "to_ref": "out:cut",
            "method": "render-cut",
            "description": "First pass cut",
        },
    )
    assert response.status_code == 200
    assert response.json()["method"] == "render-cut"

    listing = client.get(
        "/transformation-records", params={"org_id": "org-1", "brief_id": "brief-1"}
    )
    assert len(listing.json()) == 1


def test_route_missing_claim_maps_to_404(client) -> None:
    response = client.post(
        "/factuality-evaluations",
        json={
            "eval_id": "eval-1",
            "org_id": "org-1",
            "claim_id": "missing",
            "result": "PASS",
            "method_ref": "manual-review",
            "actor_ref": "reviewer-1",
        },
    )
    assert response.status_code == 404
    listing = client.get(
        "/factuality-evaluations", params={"org_id": "org-1", "claim_id": "missing"}
    )
    assert listing.status_code == 404


def test_route_missing_brief_maps_to_404(client) -> None:
    response = client.post(
        "/transformation-records",
        json={
            "transformation_id": "t-1",
            "org_id": "org-1",
            "brief_id": "missing",
            "from_ref": "src:a",
            "to_ref": "out:b",
            "method": "cut",
        },
    )
    assert response.status_code == 404


def test_route_evaluation_conflict_maps_to_409(client) -> None:
    _seed_claim_http(client)
    payload = {
        "eval_id": "eval-1",
        "org_id": "org-1",
        "claim_id": "claim-1",
        "result": "PASS",
        "method_ref": "manual-review",
        "actor_ref": "reviewer-1",
    }
    assert client.post("/factuality-evaluations", json=payload).status_code == 200
    payload["result"] = "FAIL"
    assert client.post("/factuality-evaluations", json=payload).status_code == 409
