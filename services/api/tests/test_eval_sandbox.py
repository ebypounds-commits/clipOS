"""Tests for eval sandbox and safe promotion (CLIP-084)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.eval_sandbox import (
    EvalStore,
    PromotionRequest,
    RegisterSandboxRequest,
    SandboxStatus,
    SandboxTransitionRequest,
    evaluate_promotion,
    sandbox_transition_allowed,
)
from clipos_api.improvement import (
    ImprovementStore,
    ProposalStatus,
    RegisterProposalRequest,
)
from clipos_api.main import app


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-084-eval-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> EvalStore:
    return EvalStore.from_engine(engine)


def _seed_proposal(engine: Engine, status: ProposalStatus | None = None) -> str:
    """Register a proposal; optionally advance it to the given status."""
    improvements = ImprovementStore.from_engine(engine)
    improvements.register_proposal(
        RegisterProposalRequest(
            org_id="org-1",
            proposal_id="prop-1",
            title="Improve hooks",
            weakness_ref="weakness://low-ctr",
        )
    )
    if status is not None:
        # Walk the legal path up to the requested status.
        path = {
            ProposalStatus.PROPOSED: [],
            ProposalStatus.IN_REVIEW: [ProposalStatus.IN_REVIEW],
            ProposalStatus.ACCEPTED: [ProposalStatus.IN_REVIEW, ProposalStatus.ACCEPTED],
            ProposalStatus.IMPLEMENTED: [
                ProposalStatus.IN_REVIEW,
                ProposalStatus.ACCEPTED,
                ProposalStatus.IMPLEMENTED,
            ],
        }[status]
        from clipos_api.improvement import ProposalTransitionRequest

        for step in path:
            improvements.transition_proposal(
                "prop-1",
                "org-1",
                ProposalTransitionRequest(status=step, actor_ref="op"),
            )
    return "prop-1"


def _sandbox(**overrides: object) -> RegisterSandboxRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "sandbox_id": "sb-1",
        "proposal_id": "prop-1",
        "evaluator_alias": "eval-hook-checker",
        "scenario_ref": "scenario://hooks-v2",
    }
    payload.update(overrides)
    return RegisterSandboxRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure transitions + promotion gate
# ---------------------------------------------------------------------------


def test_sandbox_lifecycle() -> None:
    assert sandbox_transition_allowed(SandboxStatus.CREATED, SandboxStatus.RUNNING)
    assert sandbox_transition_allowed(SandboxStatus.RUNNING, SandboxStatus.PASSED)
    assert sandbox_transition_allowed(SandboxStatus.RUNNING, SandboxStatus.FAILED)
    assert sandbox_transition_allowed(SandboxStatus.FAILED, SandboxStatus.RUNNING)
    assert not sandbox_transition_allowed(SandboxStatus.CREATED, SandboxStatus.PASSED)
    assert not sandbox_transition_allowed(SandboxStatus.PASSED, SandboxStatus.RUNNING)
    assert sandbox_transition_allowed(SandboxStatus.PASSED, SandboxStatus.PASSED)


def test_promotion_gate_matrix() -> None:
    allowed = evaluate_promotion(SandboxStatus.PASSED, ProposalStatus.IMPLEMENTED)
    assert allowed.allowed is True
    assert allowed.reason == "allowed"
    accepted = evaluate_promotion(SandboxStatus.PASSED, ProposalStatus.ACCEPTED)
    assert accepted.allowed is True
    not_passed = evaluate_promotion(SandboxStatus.RUNNING, ProposalStatus.IMPLEMENTED)
    assert not_passed.allowed is False
    assert not_passed.reason == "sandbox_not_passed"
    not_ready = evaluate_promotion(SandboxStatus.PASSED, ProposalStatus.PROPOSED)
    assert not_ready.allowed is False
    assert not_ready.reason == "proposal_not_ready"


# ---------------------------------------------------------------------------
# Store: sandboxes
# ---------------------------------------------------------------------------


def test_sandbox_requires_proposal(store: EvalStore) -> None:
    with pytest.raises(ValueError, match="improvement proposal not found"):
        store.register_sandbox(_sandbox())


def test_sandbox_register_idempotent_conflict(
    store: EvalStore,
    engine: Engine,
    audit_log: Path,
) -> None:
    _seed_proposal(engine)
    first = store.register_sandbox(_sandbox())
    again = store.register_sandbox(_sandbox())
    assert first.sandbox_id == again.sandbox_id
    assert first.status is SandboxStatus.CREATED
    assert first.evaluator_alias == "eval-hook-checker"
    assert "eval_sandboxes.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_sandbox(_sandbox(scenario_ref="scenario://other"))
    assert len(store.list_sandboxes("org-1")) == 1


def test_sandbox_transition_flow(
    store: EvalStore,
    engine: Engine,
    audit_log: Path,
) -> None:
    _seed_proposal(engine)
    store.register_sandbox(_sandbox())
    store.transition_sandbox(
        "sb-1", "org-1", SandboxTransitionRequest(status=SandboxStatus.RUNNING, actor_ref="op")
    )
    failed = store.transition_sandbox(
        "sb-1", "org-1", SandboxTransitionRequest(status=SandboxStatus.FAILED, actor_ref="op")
    )
    assert failed.status is SandboxStatus.FAILED
    rerun = store.transition_sandbox(
        "sb-1", "org-1", SandboxTransitionRequest(status=SandboxStatus.RUNNING, actor_ref="op")
    )
    assert rerun.status is SandboxStatus.RUNNING
    passed = store.transition_sandbox(
        "sb-1", "org-1", SandboxTransitionRequest(status=SandboxStatus.PASSED, actor_ref="op")
    )
    assert passed.status is SandboxStatus.PASSED
    assert "eval_sandboxes.status_changed" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="illegal sandbox transition"):
        store.transition_sandbox(
            "sb-1", "org-1", SandboxTransitionRequest(status=SandboxStatus.RUNNING, actor_ref="op")
        )


def test_sandbox_unknown_404(store: EvalStore) -> None:
    with pytest.raises(ValueError, match="eval sandbox not found"):
        store.transition_sandbox(
            "missing",
            "org-1",
            SandboxTransitionRequest(status=SandboxStatus.RUNNING, actor_ref="op"),
        )


# ---------------------------------------------------------------------------
# Store: promotions
# ---------------------------------------------------------------------------


def test_promotion_denied_until_passed(
    store: EvalStore,
    engine: Engine,
    audit_log: Path,
) -> None:
    _seed_proposal(engine, status=ProposalStatus.IMPLEMENTED)
    store.register_sandbox(_sandbox())

    denied = store.evaluate_and_store_promotion(
        PromotionRequest(
            org_id="org-1",
            promotion_id="prom-1",
            proposal_id="prop-1",
            sandbox_id="sb-1",
            actor_ref="op",
        )
    )
    assert denied.allowed is False
    assert denied.reason == "sandbox_not_passed"
    assert "promotions.evaluated" in audit_log.read_text(encoding="utf-8")

    # Pass the sandbox, evaluate again under a NEW promotion id.
    for status in (SandboxStatus.RUNNING, SandboxStatus.PASSED):
        store.transition_sandbox(
            "sb-1", "org-1", SandboxTransitionRequest(status=status, actor_ref="op")
        )
    allowed = store.evaluate_and_store_promotion(
        PromotionRequest(
            org_id="org-1",
            promotion_id="prom-2",
            proposal_id="prop-1",
            sandbox_id="sb-1",
            actor_ref="op",
        )
    )
    assert allowed.allowed is True
    assert allowed.reason == "allowed"
    assert len(store.list_promotions("org-1")) == 2


def test_promotion_proposal_not_ready(
    store: EvalStore,
    engine: Engine,
) -> None:
    _seed_proposal(engine, status=ProposalStatus.PROPOSED)
    store.register_sandbox(_sandbox())
    for status in (SandboxStatus.RUNNING, SandboxStatus.PASSED):
        store.transition_sandbox(
            "sb-1", "org-1", SandboxTransitionRequest(status=status, actor_ref="op")
        )
    denied = store.evaluate_and_store_promotion(
        PromotionRequest(
            org_id="org-1",
            promotion_id="prom-1",
            proposal_id="prop-1",
            sandbox_id="sb-1",
            actor_ref="op",
        )
    )
    assert denied.allowed is False
    assert denied.reason == "proposal_not_ready"


def test_promotion_idempotent_conflict(
    store: EvalStore,
    engine: Engine,
) -> None:
    _seed_proposal(engine, status=ProposalStatus.IMPLEMENTED)
    store.register_sandbox(_sandbox())
    first = store.evaluate_and_store_promotion(
        PromotionRequest(
            org_id="org-1",
            promotion_id="prom-1",
            proposal_id="prop-1",
            sandbox_id="sb-1",
            actor_ref="op",
        )
    )
    again = store.evaluate_and_store_promotion(
        PromotionRequest(
            org_id="org-1",
            promotion_id="prom-1",
            proposal_id="prop-1",
            sandbox_id="sb-1",
            actor_ref="op",
        )
    )
    assert first.promotion_id == again.promotion_id
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.evaluate_and_store_promotion(
            PromotionRequest(
                org_id="org-1",
                promotion_id="prom-1",
                proposal_id="prop-1",
                sandbox_id="sb-1",
                actor_ref="other",
            )
        )


def test_promotion_unknown_refs_404(
    store: EvalStore,
    engine: Engine,
) -> None:
    _seed_proposal(engine)
    store.register_sandbox(_sandbox())
    with pytest.raises(ValueError, match="improvement proposal not found"):
        store.evaluate_and_store_promotion(
            PromotionRequest(
                org_id="org-1",
                promotion_id="prom-x",
                proposal_id="missing",
                sandbox_id="sb-1",
                actor_ref="op",
            )
        )
    with pytest.raises(ValueError, match="eval sandbox not found"):
        store.evaluate_and_store_promotion(
            PromotionRequest(
                org_id="org-1",
                promotion_id="prom-y",
                proposal_id="prop-1",
                sandbox_id="missing",
                actor_ref="op",
            )
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/eval-sandboxes" in paths
    assert "/eval-sandboxes/{sandbox_id}/transition" in paths
    assert "/promotions" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_full_route_roundtrip(client) -> None:
    proposal = client.post(
        "/improvement-proposals",
        json={
            "org_id": "org-1",
            "proposal_id": "prop-1",
            "title": "Improve hooks",
            "weakness_ref": "weakness://low-ctr",
        },
    )
    assert proposal.status_code == 200, proposal.text
    for status in ("IN_REVIEW", "ACCEPTED", "IMPLEMENTED"):
        client.post(
            "/improvement-proposals/prop-1/transition",
            params={"org_id": "org-1"},
            json={"status": status, "actor_ref": "op"},
        )

    created = client.post(
        "/eval-sandboxes",
        json={
            "org_id": "org-1",
            "sandbox_id": "sb-1",
            "proposal_id": "prop-1",
            "evaluator_alias": "eval-hook-checker",
            "scenario_ref": "scenario://hooks-v2",
        },
    )
    assert created.status_code == 200, created.text

    listing = client.get("/eval-sandboxes", params={"org_id": "org-1"})
    assert len(listing.json()) == 1

    for status in ("RUNNING", "PASSED"):
        resp = client.post(
            "/eval-sandboxes/sb-1/transition",
            params={"org_id": "org-1"},
            json={"status": status, "actor_ref": "op"},
        )
        assert resp.status_code == 200, (status, resp.text)

    promotion = client.post(
        "/promotions",
        json={
            "org_id": "org-1",
            "promotion_id": "prom-1",
            "proposal_id": "prop-1",
            "sandbox_id": "sb-1",
            "actor_ref": "op",
        },
    )
    assert promotion.status_code == 200, promotion.text
    assert promotion.json()["allowed"] is True
    assert promotion.json()["reason"] == "allowed"

    promotions = client.get("/promotions", params={"org_id": "org-1"})
    assert len(promotions.json()) == 1


def test_route_errors(client) -> None:
    orphan = client.post(
        "/eval-sandboxes",
        json={
            "org_id": "org-1",
            "sandbox_id": "sb-x",
            "proposal_id": "missing",
            "evaluator_alias": "eval-a",
            "scenario_ref": "s",
        },
    )
    assert orphan.status_code == 404

    missing_transition = client.post(
        "/eval-sandboxes/nope/transition",
        params={"org_id": "org-1"},
        json={"status": "RUNNING", "actor_ref": "op"},
    )
    assert missing_transition.status_code == 404

    bad_slug = client.post(
        "/eval-sandboxes",
        json={
            "org_id": "org-1",
            "sandbox_id": "bad/id",
            "proposal_id": "prop-1",
            "evaluator_alias": "eval-a",
            "scenario_ref": "s",
        },
    )
    assert bad_slug.status_code == 422
