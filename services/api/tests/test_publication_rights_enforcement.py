"""Tests for rights enforcement on the publication path (P0-1).

The audit found the rights gate existed and was tested as a component but was
never consulted by `submit_publication`, so an item with no rights record
could be published. These tests pin the enforcement:

* the pure evaluator refuses without `rights_clear` (fail-closed default);
* the route consults the gate and DENIES an uncleared item;
* a CLEAR item with provenance publishes normally (no false positive);
* revocation re-blocks publication;
* the rights reason is surfaced, not a generic denial.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.publishing import (
    OperatingMode,
    PublishPolicy,
    Visibility,
    evaluate_publish_policy,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-p01-rights-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _policy(**overrides: object) -> PublishPolicy:
    payload: dict[str, object] = {
        "policy_id": "p01-pol",
        "org_id": "org-1",
        "platform": "youtube",
        "allowed_visibilities": [Visibility.PRIVATE, Visibility.UNLISTED, Visibility.PUBLIC],
        "public_enabled": True,
        "kill_switch": False,
    }
    payload.update(overrides)
    return PublishPolicy(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure evaluator: fail-closed default
# ---------------------------------------------------------------------------


def test_evaluator_refuses_without_rights_clear() -> None:
    """Omitting the rights gate must deny — the fail-closed default."""
    allowed, reason = evaluate_publish_policy(
        _policy(), Visibility.PRIVATE, OperatingMode.PRIVATE_TEST
    )
    assert allowed is False
    assert reason == "rights_not_clear"


def test_evaluator_allows_private_when_rights_clear() -> None:
    allowed, reason = evaluate_publish_policy(
        _policy(), Visibility.PRIVATE, OperatingMode.PRIVATE_TEST, False, True
    )
    assert allowed is True
    assert reason == "allowed"


def test_evaluator_rights_check_precedes_policy_rules() -> None:
    """Rights is evaluated first, so a kill-switched policy still names rights."""
    allowed, reason = evaluate_publish_policy(
        _policy(kill_switch=True), Visibility.PRIVATE, OperatingMode.PRIVATE_TEST, False, False
    )
    assert allowed is False
    assert reason == "rights_not_clear"


def test_evaluator_kill_switch_still_wins_once_rights_clear() -> None:
    allowed, reason = evaluate_publish_policy(
        _policy(kill_switch=True), Visibility.PRIVATE, OperatingMode.PRIVATE_TEST, False, True
    )
    assert allowed is False
    assert reason == "kill_switch_active"


def test_evaluator_public_still_needs_owner_approval_with_rights_clear() -> None:
    allowed, reason = evaluate_publish_policy(
        _policy(), Visibility.PUBLIC, OperatingMode.GUARDED_PUBLIC, False, True
    )
    assert allowed is False
    assert reason == "owner_approval_required"


# ---------------------------------------------------------------------------
# Route: the enforcement path
# ---------------------------------------------------------------------------


def _register_policy(client: TestClient, platform: str = "youtube") -> None:
    created = client.post(
        "/publish-policies",
        json={
            "policy_id": f"p01-{platform}",
            "org_id": "org-1",
            "platform": platform,
            "allowed_visibilities": ["PRIVATE", "UNLISTED"],
            "public_enabled": False,
        },
    )
    assert created.status_code == 200, created.text


def _publish(client: TestClient, content_ref: str, key: str, rights_item_id: str | None = None):
    body = {
        "org_id": "org-1",
        "platform": "youtube",
        "account_ref": "yt://p01",
        "content_ref": content_ref,
        "title": "p0-1 rights probe",
        "visibility": "PRIVATE",
        "idempotency_key": key,
        "operating_mode": "PRIVATE_TEST",
    }
    if rights_item_id is not None:
        body["rights_item_id"] = rights_item_id
    return client.post("/publications", json=body)


def test_publication_denied_without_rights_record(client: TestClient) -> None:
    """The core fix: an uncleared item cannot be published at ANY visibility."""
    _register_policy(client)
    response = _publish(client, "storage://never-cleared", "p01-key-1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "DENIED"
    assert body["decision_reason"].startswith("rights_not_clear")


def test_publication_denial_names_the_rights_reason(client: TestClient) -> None:
    """The reason must name the gate verdict, not a generic refusal."""
    _register_policy(client)
    reason = _publish(client, "storage://another-uncleared", "p01-key-2").json()["decision_reason"]
    assert "rights" in reason
    # The gate reports not_found for an unregistered item; the reason carries it.
    assert "not_found" in reason or reason == "rights_not_clear"


def test_publication_allowed_when_rights_clear(client: TestClient, clear_rights) -> None:
    """No false positive: a cleared item with provenance publishes."""
    _register_policy(client)
    clear_rights("storage://cleared-item")
    response = _publish(client, "storage://cleared-item", "p01-key-3")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "SUCCEEDED", body
    assert body["denial_reasons"] == []


def test_explicit_rights_item_id_is_honoured(client: TestClient, clear_rights) -> None:
    """A submission may name its rights item explicitly."""
    _register_policy(client)
    clear_rights("rights-item-xyz")
    response = _publish(
        client, "storage://opaque-ref", "p01-key-4", rights_item_id="rights-item-xyz"
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "SUCCEEDED"


def test_revocation_reblocks_publication(client: TestClient, engine: Engine, clear_rights) -> None:
    """Revoking rights must close publication for that item again."""
    from clipos_api.rights_ledger import RightsLedgerStore, Status

    _register_policy(client)
    record_id = clear_rights("storage://revocable")
    assert _publish(client, "storage://revocable", "p01-key-5").json()["status"] == "SUCCEEDED"

    RightsLedgerStore.from_engine(engine).record_status_change(
        record_id, "org-1", Status.REVOKED, "reviewer:audit"
    )
    after = _publish(client, "storage://revocable", "p01-key-6")
    assert after.json()["status"] == "DENIED"
    assert after.json()["decision_reason"].startswith("rights_not_clear")


def test_denied_job_is_persisted_and_audited(client: TestClient, audit_log: Path) -> None:
    _register_policy(client)
    response = _publish(client, "storage://audit-check", "p01-key-7")
    job_id = response.json()["job_id"]
    fetched = client.get(f"/publications/{job_id}", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "DENIED"
    assert "publication_jobs.denied" in audit_log.read_text(encoding="utf-8")


def test_idempotent_replay_returns_the_denied_job(client: TestClient) -> None:
    """A repeat submission must not re-evaluate into a different verdict."""
    _register_policy(client)
    first = _publish(client, "storage://replay", "p01-key-8").json()
    second = _publish(client, "storage://replay", "p01-key-8").json()
    assert first["job_id"] == second["job_id"]
    assert second["status"] == "DENIED"


def test_rights_gate_error_fails_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the gate cannot be evaluated, publication must be refused."""
    from clipos_api import publishing

    def broken_gate(request, engine):
        return False, "rights_gate_error:RuntimeError"

    monkeypatch.setattr(publishing, "_rights_cleared", broken_gate)
    _register_policy(client)
    response = _publish(client, "storage://gate-broken", "p01-key-9")
    assert response.json()["status"] == "DENIED"
    assert "rights_gate_error" in response.json()["decision_reason"]


# ---------------------------------------------------------------------------
# Every failed precondition, not just the first
# ---------------------------------------------------------------------------


def test_conditions_collect_every_failed_precondition() -> None:
    """The evaluator reports all failed preconditions, in precedence order."""
    from clipos_api.publishing import evaluate_publish_conditions

    decision = evaluate_publish_conditions(
        _policy(kill_switch=True),
        Visibility.PUBLIC,
        OperatingMode.GUARDED_PUBLIC,
        account_approved=False,
        rights_clear=False,
    )
    assert decision.allowed is False
    assert decision.reason == "rights_not_clear"
    assert "rights_not_clear" in decision.reasons
    assert "kill_switch_active" in decision.reasons
    assert "owner_approval_required" in decision.reasons
    # The primary reason is the first in precedence order.
    assert decision.reasons[0] == decision.reason


def test_denial_lists_every_failed_precondition(client: TestClient) -> None:
    """A denied job carries the full set, not only the first failure."""
    client.post(
        "/publish-policies",
        json={
            "policy_id": "p01-kill",
            "org_id": "org-1",
            "platform": "youtube",
            "allowed_visibilities": ["PRIVATE", "UNLISTED", "PUBLIC"],
            "public_enabled": True,
            "kill_switch": True,
        },
    )
    body = _publish(client, "storage://multi-fail", "p01-key-10").json()
    assert body["status"] == "DENIED"
    reasons = body["denial_reasons"]
    assert reasons, "the denial must carry at least one reason"
    assert any("rights" in reason for reason in reasons)
    assert "kill_switch_active" in reasons
    assert body["decision_reason"] == reasons[0]


def test_denial_reasons_are_persisted(client: TestClient) -> None:
    """The stored job reports the same reasons as the submission did."""
    _register_policy(client)
    body = _publish(client, "storage://persisted", "p01-key-11").json()
    fetched = client.get(f"/publications/{body['job_id']}", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert fetched.json()["denial_reasons"] == body["denial_reasons"]
    assert fetched.json()["decision_reason"] == body["decision_reason"]
