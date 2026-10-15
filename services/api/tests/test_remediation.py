"""Tests for safe auto-remediation actions (CLIP-088)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.remediation import (
    MockRemediationExecutor,
    RegisterActionRequest,
    RemediationAction,
    RemediationOutcome,
    RemediationStore,
    RunRemediationRequest,
    evaluate_remediation,
    template_placeholders,
    validate_command_template,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-088-remediation-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> RemediationStore:
    return RemediationStore.from_engine(engine)


def _action(**overrides: object) -> RegisterActionRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "action_id": "act-1",
        "action_kind": "restart-service",
        "command_template": "svc restart {service} --grace {seconds}",
        "max_attempts": 3,
        "requires_approval": True,
        "enabled": True,
    }
    payload.update(overrides)
    return RegisterActionRequest(**payload)  # type: ignore[arg-type]


def _run(**overrides: object) -> RunRemediationRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "run_id": "run-1",
        "params": {"service": "api", "seconds": "30"},
        "actor_ref": "op-1",
        "approved": True,
    }
    payload.update(overrides)
    return RunRemediationRequest(**payload)  # type: ignore[arg-type]


def _model(**overrides: object) -> RemediationAction:
    payload: dict[str, object] = {
        "action_id": "act-1",
        "org_id": "org-1",
        "action_kind": "restart-service",
        "command_template": "svc restart {service}",
        "max_attempts": 3,
        "requires_approval": True,
        "enabled": True,
    }
    payload.update(overrides)
    return RemediationAction(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Template validation (pure)
# ---------------------------------------------------------------------------


def test_template_placeholders() -> None:
    assert template_placeholders("svc restart {service} --grace {seconds}") == [
        "service",
        "seconds",
    ]
    assert template_placeholders("health-check") == []


def test_template_rejects_shell_metacharacters() -> None:
    problems = validate_command_template("svc restart {service}; rm -rf /")
    assert "template_has_shell_metacharacter" in problems
    assert validate_command_template("svc restart {service} $(whoami)") != []


def test_template_rejects_bad_placeholder() -> None:
    problems = validate_command_template("svc restart {Service Name}")
    assert any(p.startswith("bad_placeholder") for p in problems)


def test_template_static_command_ok() -> None:
    assert validate_command_template("health-check --all") == []


# ---------------------------------------------------------------------------
# Pure gate
# ---------------------------------------------------------------------------


def test_gate_allows_when_all_conditions_met() -> None:
    decision = evaluate_remediation(_model(), {"service": "api"}, 0, approved=True)
    assert decision.allowed is True
    assert decision.rendered_command == "svc restart api"
    assert decision.reason == "allowed"


def test_gate_denies_kill_switch_first() -> None:
    decision = evaluate_remediation(
        _model(), {"service": "api"}, 0, approved=True, kill_switch=True
    )
    assert decision.allowed is False
    assert decision.reason == "kill_switch_active"


def test_gate_denies_disabled_action() -> None:
    decision = evaluate_remediation(_model(enabled=False), {"service": "api"}, 0, approved=True)
    assert decision.reason == "action_disabled"


def test_gate_denies_missing_or_extra_params() -> None:
    missing = evaluate_remediation(_model(), {}, 0, approved=True)
    assert missing.reason == "params_invalid"
    extra = evaluate_remediation(_model(), {"service": "api", "other": "x"}, 0, approved=True)
    assert extra.reason == "params_invalid"


def test_gate_rejects_injection_attempts() -> None:
    for value in (
        "api; rm -rf /",
        "api && curl evil",
        "api|cat /etc/passwd",
        "$(whoami)",
        "`id`",
        "{other}",
        "api\nrm",
        "",
        "a" * 200,
    ):
        decision = evaluate_remediation(_model(), {"service": value}, 0, approved=True)
        assert decision.allowed is False, value
        assert decision.reason == "params_invalid"


def test_gate_requires_approval_when_flagged() -> None:
    decision = evaluate_remediation(_model(), {"service": "api"}, 0, approved=False)
    assert decision.reason == "approval_required"
    no_approval = evaluate_remediation(
        _model(requires_approval=False), {"service": "api"}, 0, approved=False
    )
    assert no_approval.allowed is True


def test_gate_rate_limited_at_cap() -> None:
    decision = evaluate_remediation(_model(max_attempts=2), {"service": "api"}, 2, approved=True)
    assert decision.reason == "rate_limited"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_register_action_idempotent_conflict_audit(
    store: RemediationStore,
    audit_log: Path,
) -> None:
    first = store.register_action(_action())
    again = store.register_action(_action())
    assert first.action_id == again.action_id
    assert "remediation_actions.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_action(_action(max_attempts=5))
    assert len(store.list_actions("org-1")) == 1


def test_register_action_rejects_bad_template(store: RemediationStore) -> None:
    with pytest.raises(ValueError, match="invalid command template"):
        store.register_action(_action(command_template="svc restart {service}; rm -rf /"))


def test_run_remediation_success(
    store: RemediationStore,
    audit_log: Path,
) -> None:
    store.register_action(_action())
    run = store.run_remediation("act-1", _run(), MockRemediationExecutor())
    assert run.outcome is RemediationOutcome.SUCCEEDED
    assert run.rendered_command == "svc restart api --grace 30"
    assert "remediation_runs.evaluated" in audit_log.read_text(encoding="utf-8")


def test_run_remediation_denied_never_executes(store: RemediationStore) -> None:
    store.register_action(_action())
    calls: list[str] = []

    class RecordingExecutor:
        def run(self, action_kind: str, rendered: str) -> tuple[bool, str]:
            calls.append(rendered)
            return True, "executed"

    denied = store.run_remediation("act-1", _run(approved=False), RecordingExecutor())
    assert denied.outcome is RemediationOutcome.DENIED
    assert denied.reason == "approval_required"
    assert denied.rendered_command == ""
    assert calls == []

    killed = store.run_remediation(
        "act-1", _run(run_id="run-kill", kill_switch=True), RecordingExecutor()
    )
    assert killed.reason == "kill_switch_active"
    assert calls == []

    injected = store.run_remediation(
        "act-1",
        _run(run_id="run-inject", params={"service": "api; rm -rf /", "seconds": "1"}),
        RecordingExecutor(),
    )
    assert injected.outcome is RemediationOutcome.DENIED
    assert calls == []


def test_run_remediation_rate_cap(store: RemediationStore) -> None:
    store.register_action(_action(max_attempts=2, requires_approval=False))
    for index in range(2):
        outcome = store.run_remediation(
            "act-1",
            _run(run_id=f"run-{index}", approved=True),
            MockRemediationExecutor(),
        )
        assert outcome.outcome is RemediationOutcome.SUCCEEDED
    capped = store.run_remediation(
        "act-1", _run(run_id="run-cap", approved=True), MockRemediationExecutor()
    )
    assert capped.outcome is RemediationOutcome.DENIED
    assert capped.reason == "rate_limited"


def test_run_remediation_executor_failure_recorded(store: RemediationStore) -> None:
    store.register_action(_action())

    class BrokenExecutor:
        def run(self, action_kind: str, rendered: str) -> tuple[bool, str]:
            raise RuntimeError("host unreachable")

    run = store.run_remediation("act-1", _run(), BrokenExecutor())
    assert run.outcome is RemediationOutcome.FAILED
    assert run.reason.startswith("executor_error")


def test_run_remediation_idempotent_conflict(store: RemediationStore) -> None:
    store.register_action(_action())
    first = store.run_remediation("act-1", _run(), MockRemediationExecutor())
    again = store.run_remediation("act-1", _run(), MockRemediationExecutor())
    assert first.run_id == again.run_id
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.run_remediation("act-1", _run(actor_ref="other"), MockRemediationExecutor())


def test_run_unknown_action_404(store: RemediationStore) -> None:
    with pytest.raises(ValueError, match="remediation action not found"):
        store.run_remediation("missing", _run(), MockRemediationExecutor())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/remediation-actions" in paths
    assert "/remediation-actions/{action_id}/run" in paths
    assert "/remediation-runs" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _action_payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "action_id": "act-1",
        "action_kind": "restart-service",
        "command_template": "svc restart {service}",
        "max_attempts": 3,
        "requires_approval": True,
    }


def test_full_route_roundtrip(client) -> None:
    created = client.post("/remediation-actions", json=_action_payload())
    assert created.status_code == 200, created.text

    listing = client.get("/remediation-actions", params={"org_id": "org-1"})
    assert len(listing.json()) == 1

    approved = client.post(
        "/remediation-actions/act-1/run",
        json={
            "org_id": "org-1",
            "run_id": "run-1",
            "params": {"service": "api"},
            "actor_ref": "op-1",
            "approved": True,
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["outcome"] == "SUCCEEDED"
    assert approved.json()["rendered_command"] == "svc restart api"

    denied = client.post(
        "/remediation-actions/act-1/run",
        json={
            "org_id": "org-1",
            "run_id": "run-2",
            "params": {"service": "api"},
            "actor_ref": "op-1",
            "approved": False,
        },
    )
    assert denied.status_code == 200
    assert denied.json()["outcome"] == "DENIED"

    runs = client.get("/remediation-runs", params={"org_id": "org-1"})
    assert len(runs.json()) == 2


def test_route_errors(client) -> None:
    bad_template = client.post(
        "/remediation-actions",
        json={**_action_payload(), "command_template": "svc restart {service}; rm -rf /"},
    )
    assert bad_template.status_code == 422

    missing = client.post(
        "/remediation-actions/nope/run",
        json={"org_id": "org-1", "run_id": "r", "actor_ref": "op"},
    )
    assert missing.status_code == 404

    client.post("/remediation-actions", json=_action_payload())
    conflict = client.post("/remediation-actions", json={**_action_payload(), "max_attempts": 5})
    assert conflict.status_code == 409

    bad_slug = client.post(
        "/remediation-actions", json={**_action_payload(), "action_id": "bad/id"}
    )
    assert bad_slug.status_code == 422
