"""Tests for owner-gated public publishing (CLIP-099).

Covers the pure gate, the store, and — most importantly — that the
publication path itself refuses PUBLIC without a live owner approval.
Public posting to a real account is the human gate for this ticket; these
tests exercise the guard against the Mock adapter only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.public_publishing_gate import (
    ApprovalStatus,
    GateDecision,
    GateReason,
    GrantApprovalRequest,
    OwnerApproval,
    PrivateTest,
    PrivateTestOutcome,
    PublicGateStore,
    RecordPrivateTestRequest,
    RevokeApprovalRequest,
    evaluate_public_gate,
    gate_summary,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-099-gate-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> PublicGateStore:
    return PublicGateStore.from_engine(engine)


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _approval(**overrides: object) -> OwnerApproval:
    payload: dict[str, object] = {
        "approval_id": "appr-1",
        "org_id": "org-1",
        "platform": "youtube",
        "account_ref": "yt://main",
        "status": ApprovalStatus.GRANTED,
        "owner_ref": "owner://alice",
        "private_test_ref": "pt-1",
        "note": "",
        "granted_at": datetime.now(UTC),
        "revoked_at": None,
        "expires_at": None,
    }
    payload.update(overrides)
    return OwnerApproval(**payload)  # type: ignore[arg-type]


def _passed_test(**overrides: object) -> PrivateTest:
    payload: dict[str, object] = {
        "test_ref": "pt-1",
        "org_id": "org-1",
        "platform": "youtube",
        "account_ref": "yt://main",
        "outcome": PrivateTestOutcome.PASSED,
        "detail": "",
        "recorded_at": datetime.now(UTC),
    }
    payload.update(overrides)
    return PrivateTest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure gate
# ---------------------------------------------------------------------------


def test_no_approval_is_denied() -> None:
    result = evaluate_public_gate([], [], "yt://main", datetime.now(UTC))
    assert result.decision is GateDecision.DENIED
    assert result.reason is GateReason.NO_APPROVAL


def test_approval_without_private_test_is_denied() -> None:
    result = evaluate_public_gate([_approval()], [], "yt://main", datetime.now(UTC))
    assert result.decision is GateDecision.DENIED
    assert result.reason is GateReason.NO_PRIVATE_TEST


def test_failed_private_test_does_not_unlock_the_gate() -> None:
    result = evaluate_public_gate(
        [_approval()],
        [_passed_test(outcome=PrivateTestOutcome.FAILED)],
        "yt://main",
        datetime.now(UTC),
    )
    assert result.decision is GateDecision.DENIED
    assert result.reason is GateReason.NO_PRIVATE_TEST


def test_approval_after_passing_private_test_is_allowed() -> None:
    result = evaluate_public_gate([_approval()], [_passed_test()], "yt://main", datetime.now(UTC))
    assert result.decision is GateDecision.ALLOWED
    assert result.reason is GateReason.ALLOWED
    assert result.approval_id == "appr-1"


def test_revoked_approval_is_denied() -> None:
    result = evaluate_public_gate(
        [_approval(status=ApprovalStatus.REVOKED)],
        [_passed_test()],
        "yt://main",
        datetime.now(UTC),
    )
    assert result.decision is GateDecision.DENIED
    assert result.reason is GateReason.APPROVAL_REVOKED


def test_expired_approval_is_denied() -> None:
    now = datetime.now(UTC)
    result = evaluate_public_gate(
        [_approval(expires_at=now - timedelta(hours=1))],
        [_passed_test()],
        "yt://main",
        now,
    )
    assert result.decision is GateDecision.DENIED
    assert result.reason is GateReason.APPROVAL_EXPIRED


def test_approval_expiring_in_the_future_is_allowed() -> None:
    now = datetime.now(UTC)
    result = evaluate_public_gate(
        [_approval(expires_at=now + timedelta(days=30))],
        [_passed_test()],
        "yt://main",
        now,
    )
    assert result.decision is GateDecision.ALLOWED


def test_approval_for_one_account_does_not_unlock_another() -> None:
    """The gate is per real platform account, not per org."""
    result = evaluate_public_gate(
        [_approval()], [_passed_test()], "yt://second-channel", datetime.now(UTC)
    )
    assert result.decision is GateDecision.DENIED
    assert result.reason is GateReason.NO_APPROVAL


def test_latest_approval_decides() -> None:
    """A re-granted approval supersedes an earlier revoked one."""
    now = datetime.now(UTC)
    result = evaluate_public_gate(
        [
            _approval(
                approval_id="old", status=ApprovalStatus.REVOKED, granted_at=now - timedelta(days=2)
            ),
            _approval(approval_id="new", granted_at=now),
        ],
        [_passed_test()],
        "yt://main",
        now,
    )
    assert result.decision is GateDecision.ALLOWED
    assert result.approval_id == "new"


def test_summary_counts_allowed_and_denied() -> None:
    now = datetime.now(UTC)
    summary = gate_summary(
        "org-1",
        [_approval()],
        [
            _passed_test(),
            _passed_test(test_ref="pt-2", account_ref="yt://other"),
        ],
        now,
    )
    assert summary.allowed_count == 1
    assert summary.denied_count == 1
    denied = [a for a in summary.accounts if a.decision is GateDecision.DENIED]
    assert denied[0].reason is GateReason.NO_APPROVAL


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _grant(**overrides: object) -> GrantApprovalRequest:
    payload: dict[str, object] = {
        "approval_id": "appr-1",
        "org_id": "org-1",
        "platform": "youtube",
        "account_ref": "yt://main",
        "owner_ref": "owner://alice",
        "private_test_ref": "pt-1",
        "note": "approved after private test",
    }
    payload.update(overrides)
    return GrantApprovalRequest(**payload)  # type: ignore[arg-type]


def test_grant_approval_is_idempotent_and_audited(store: PublicGateStore, audit_log: Path) -> None:
    first = store.grant_approval(_grant())
    second = store.grant_approval(_grant())
    assert first == second
    assert first.status is ApprovalStatus.GRANTED
    assert "owner_approvals.granted" in audit_log.read_text(encoding="utf-8")


def test_grant_approval_rejects_field_drift(store: PublicGateStore) -> None:
    store.grant_approval(_grant())
    with pytest.raises(ValueError, match="different fields"):
        store.grant_approval(_grant(owner_ref="owner://bob"))


def test_revoke_approval_is_idempotent_and_audited(store: PublicGateStore, audit_log: Path) -> None:
    store.grant_approval(_grant())
    revoked = store.revoke_approval(
        "appr-1", RevokeApprovalRequest(org_id="org-1", owner_ref="owner://alice")
    )
    assert revoked.status is ApprovalStatus.REVOKED
    assert revoked.revoked_at is not None
    again = store.revoke_approval(
        "appr-1", RevokeApprovalRequest(org_id="org-1", owner_ref="owner://alice")
    )
    assert again.revoked_at == revoked.revoked_at
    assert "owner_approvals.revoked" in audit_log.read_text(encoding="utf-8")


def test_revoke_unknown_approval_is_404(store: PublicGateStore) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.revoke_approval(
            "ghost", RevokeApprovalRequest(org_id="org-1", owner_ref="owner://alice")
        )


def test_record_private_test_is_idempotent(store: PublicGateStore, audit_log: Path) -> None:
    request = RecordPrivateTestRequest(
        test_ref="pt-1",
        org_id="org-1",
        platform="youtube",
        account_ref="yt://main",
        outcome=PrivateTestOutcome.PASSED,
    )
    first = store.record_private_test(request)
    second = store.record_private_test(request)
    assert first == second
    assert "private_tests.recorded" in audit_log.read_text(encoding="utf-8")


def test_record_private_test_rejects_field_drift(store: PublicGateStore) -> None:
    store.record_private_test(
        RecordPrivateTestRequest(
            test_ref="pt-1",
            org_id="org-1",
            platform="youtube",
            account_ref="yt://main",
            outcome=PrivateTestOutcome.PASSED,
        )
    )
    with pytest.raises(ValueError, match="different fields"):
        store.record_private_test(
            RecordPrivateTestRequest(
                test_ref="pt-1",
                org_id="org-1",
                platform="youtube",
                account_ref="yt://main",
                outcome=PrivateTestOutcome.FAILED,
            )
        )


def test_store_decide_requires_both_halves(store: PublicGateStore) -> None:
    store.grant_approval(_grant())
    assert store.decide("org-1", "yt://main").decision is GateDecision.DENIED
    store.record_private_test(
        RecordPrivateTestRequest(
            test_ref="pt-1",
            org_id="org-1",
            platform="youtube",
            account_ref="yt://main",
            outcome=PrivateTestOutcome.PASSED,
        )
    )
    assert store.decide("org-1", "yt://main").decision is GateDecision.ALLOWED


def test_models_are_json_serializable(store: PublicGateStore) -> None:
    approval = store.grant_approval(_grant())
    json.dumps(approval.model_dump(mode="json"))
    summary = store.summary("org-1")
    json.dumps(summary.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# The publication path must honour the gate
# ---------------------------------------------------------------------------


def _policy(client: TestClient, *, public_enabled: bool) -> None:
    created = client.post(
        "/publish-policies",
        json={
            "policy_id": "gate-pol",
            "org_id": "org-1",
            "platform": "youtube",
            "allowed_visibilities": ["PRIVATE", "UNLISTED", "PUBLIC"],
            "public_enabled": public_enabled,
        },
    )
    assert created.status_code == 200, created.text


def _submit_public(client: TestClient, key: str = "gate-pub-1") -> object:
    return client.post(
        "/publications",
        json={
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt://main",
            "content_ref": "storage://content",
            "title": "gate probe",
            "visibility": "PUBLIC",
            "idempotency_key": key,
            "operating_mode": "GUARDED_PUBLIC",
        },
    )


@pytest.fixture()
def cleared_content_rights(clear_rights) -> str:
    """Put the published item in the CLEAR rights state (CLIP-033).

    The publication path requires cleared rights for every visibility. These
    tests are about the *owner* gate, so the rights precondition is
    established first and the owner gate is the reason under test.
    """
    return clear_rights("storage://content", org_id="org-1")


def test_public_denied_without_owner_approval(
    client: TestClient, cleared_content_rights: str
) -> None:
    """Even with public_enabled, PUBLIC must be refused with no approval."""
    _policy(client, public_enabled=True)
    response = _submit_public(client)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "DENIED"
    assert body["decision_reason"] == "owner_approval_required"


def test_public_denied_with_approval_but_no_private_test(
    client: TestClient, cleared_content_rights: str
) -> None:
    _policy(client, public_enabled=True)
    client.post(
        "/publishing/owner-approvals",
        json={
            "approval_id": "appr-1",
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt://main",
            "owner_ref": "owner://alice",
            "private_test_ref": "pt-1",
        },
    )
    response = _submit_public(client)
    assert response.json()["decision_reason"] == "owner_approval_required"


def test_public_allowed_once_approved_after_a_passing_private_test(
    client: TestClient,
    cleared_content_rights: str,
) -> None:
    _policy(client, public_enabled=True)
    client.post(
        "/publishing/private-tests",
        json={
            "test_ref": "pt-1",
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt://main",
            "outcome": "PASSED",
        },
    )
    client.post(
        "/publishing/owner-approvals",
        json={
            "approval_id": "appr-1",
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt://main",
            "owner_ref": "owner://alice",
            "private_test_ref": "pt-1",
        },
    )
    response = _submit_public(client)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SUCCEEDED", body
    assert body["visibility"] == "PUBLIC"


def test_public_denied_again_after_revocation(
    client: TestClient, cleared_content_rights: str
) -> None:
    """Revoking the approval must close public publishing for that account."""
    _policy(client, public_enabled=True)
    client.post(
        "/publishing/private-tests",
        json={
            "test_ref": "pt-1",
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt://main",
            "outcome": "PASSED",
        },
    )
    client.post(
        "/publishing/owner-approvals",
        json={
            "approval_id": "appr-1",
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt://main",
            "owner_ref": "owner://alice",
            "private_test_ref": "pt-1",
        },
    )
    assert _submit_public(client, "gate-pub-2").json()["status"] == "SUCCEEDED"

    revoked = client.post(
        "/publishing/owner-approvals/appr-1/revoke",
        json={"org_id": "org-1", "owner_ref": "owner://alice"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "REVOKED"

    after = _submit_public(client, "gate-pub-3")
    assert after.json()["status"] == "DENIED"
    assert after.json()["decision_reason"] == "owner_approval_required"


def test_private_visibility_does_not_consult_the_gate(
    client: TestClient, cleared_content_rights: str
) -> None:
    """A private post is unaffected by the absence of an owner approval."""
    _policy(client, public_enabled=False)
    response = client.post(
        "/publications",
        json={
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt://main",
            "content_ref": "storage://content",
            "title": "private probe",
            "visibility": "PRIVATE",
            "idempotency_key": "gate-private-1",
            "operating_mode": "PRIVATE_TEST",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "SUCCEEDED"


def test_gate_summary_route(client: TestClient) -> None:
    client.post(
        "/publishing/private-tests",
        json={
            "test_ref": "pt-1",
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt://main",
            "outcome": "PASSED",
        },
    )
    summary = client.get("/publishing/gate-summary", params={"org_id": "org-1"})
    assert summary.status_code == 200
    body = summary.json()
    assert body["denied_count"] == 1
    assert body["accounts"][0]["reason"] == "no_owner_approval"


def test_routes_are_registered() -> None:
    from fastapi.routing import APIRoute

    from clipos_api.main import app

    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    for expected in (
        "/publishing/owner-approvals",
        "/publishing/owner-approvals/{approval_id}/revoke",
        "/publishing/private-tests",
        "/publishing/gate-summary",
    ):
        assert expected in paths, f"missing route: {expected}"
