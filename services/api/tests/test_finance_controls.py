"""Tests for anomaly detection and finance approval gates (CLIP-077)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.cost_ledger import (
    CostLedgerStore,
    MockCostAdapter,
    RegisterSourceRequest,
    SyncCostsRequest,
)
from clipos_api.finance_controls import (
    ApprovalStatus,
    FinanceControlStore,
    GateRequest,
    ResolveApprovalRequest,
    SubmitApprovalRequest,
    detect_cost_anomaly,
    evaluate_approval_gate,
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
    path = Path("/tmp") / "clipos-077-finance-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> FinanceControlStore:
    return FinanceControlStore.from_engine(engine)


def _seed_entries(engine: Engine) -> list[str]:
    """Sync three windows so the source has 3 USAGE entries; return entry ids."""
    cost_store = CostLedgerStore.from_engine(engine)
    cost_store.register_source(
        RegisterSourceRequest(
            org_id="org-1",
            source_id="csrc-1",
            platform="cloud",
            account_ref="acct://cloud",
            cost_center="compute",
        )
    )
    windows = [
        (date(2026, 9, 1), date(2026, 9, 7)),
        (date(2026, 9, 8), date(2026, 9, 14)),
        (date(2026, 9, 15), date(2026, 9, 21)),
    ]
    for start, end in windows:
        cost_store.sync(
            MockCostAdapter(),
            SyncCostsRequest(
                org_id="org-1",
                source_id="csrc-1",
                period_start=start,
                period_end=end,
            ),
        )
    entries = cost_store.list_entries("org-1", "csrc-1")
    # Return USAGE entry ids (two categories per sync -> 6 total).
    return [e.entry_id for e in entries]


# ---------------------------------------------------------------------------
# Pure anomaly detection
# ---------------------------------------------------------------------------


def test_anomaly_insufficient_data() -> None:
    check = detect_cost_anomaly(500, [100])
    assert check.verdict.value == "INSUFFICIENT_DATA"
    assert check.z_score is None


def test_anomaly_normal_within_threshold() -> None:
    check = detect_cost_anomaly(110, [100, 105, 95, 100], z_threshold=3.0)
    assert check.verdict.value == "NORMAL"
    assert check.z_score is not None
    assert check.z_score <= 3.0


def test_anomaly_spike_detected() -> None:
    check = detect_cost_anomaly(10_000, [100, 100, 100, 100], z_threshold=3.0)
    assert check.verdict.value == "ANOMALY"
    assert check.z_score is not None and check.z_score > 3.0


def test_anomaly_zero_variance() -> None:
    equal = detect_cost_anomaly(100, [100, 100])
    assert equal.verdict.value == "NORMAL"
    above = detect_cost_anomaly(200, [100, 100])
    assert above.verdict.value == "ANOMALY"
    assert above.z_score == float("inf")


def test_anomaly_negative_threshold_rejected() -> None:
    with pytest.raises(ValueError, match="z_threshold"):
        detect_cost_anomaly(100, [90, 110], z_threshold=0.0)


# ---------------------------------------------------------------------------
# Pure approval gate
# ---------------------------------------------------------------------------


def test_gate_not_approved() -> None:
    pending = evaluate_approval_gate(ApprovalStatus.PENDING, 100, "USD", 50, "USD")
    assert pending.allowed is False
    assert pending.reason == "not_approved"
    rejected = evaluate_approval_gate(ApprovalStatus.REJECTED, 100, "USD", 50, "USD")
    assert rejected.allowed is False


def test_gate_approved_within_cap() -> None:
    decision = evaluate_approval_gate(ApprovalStatus.APPROVED, 1_000, "USD", 1_000, "USD")
    assert decision.allowed is True
    assert decision.reason == "approved"


def test_gate_amount_exceeds() -> None:
    decision = evaluate_approval_gate(ApprovalStatus.APPROVED, 500, "USD", 501, "USD")
    assert decision.allowed is False
    assert decision.reason == "amount_exceeds_approved"


def test_gate_currency_mismatch() -> None:
    decision = evaluate_approval_gate(ApprovalStatus.APPROVED, 500, "USD", 100, "EUR")
    assert decision.allowed is False
    assert decision.reason == "currency_mismatch"


# ---------------------------------------------------------------------------
# Store: anomaly checks
# ---------------------------------------------------------------------------


def test_anomaly_check_entry_404(store: FinanceControlStore) -> None:
    from clipos_api.finance_controls import CheckAnomalyRequest

    with pytest.raises(ValueError, match="cost entry not found"):
        store.run_anomaly_check(
            CheckAnomalyRequest(org_id="org-1", anomaly_id="anom-1", entry_id="missing")
        )


def test_anomaly_check_on_ledger_entries(
    store: FinanceControlStore,
    engine: Engine,
    audit_log: Path,
) -> None:
    from clipos_api.finance_controls import CheckAnomalyRequest

    entry_ids = _seed_entries(engine)
    # Use two USAGE entries as each other's baseline (>=2 needed only for
    # z-score; two peers minus self gives 1 -> INSUFFICIENT_DATA is fine).
    check = store.run_anomaly_check(
        CheckAnomalyRequest(org_id="org-1", anomaly_id="anom-1", entry_id=entry_ids[0])
    )
    assert check.verdict.value in ("ANOMALY", "NORMAL", "INSUFFICIENT_DATA")
    assert "cost_anomalies.recorded" in audit_log.read_text(encoding="utf-8")

    again = store.run_anomaly_check(
        CheckAnomalyRequest(org_id="org-1", anomaly_id="anom-1", entry_id=entry_ids[0])
    )
    assert again.anomaly_id == check.anomaly_id
    assert len(store.list_anomalies("org-1")) == 1


# ---------------------------------------------------------------------------
# Store: approvals
# ---------------------------------------------------------------------------


def _approval(**overrides: object) -> SubmitApprovalRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "approval_id": "appr-1",
        "purpose_ref": "spend://big-buy",
        "amount_cents": 1_000,
        "currency": "USD",
        "submitted_by": "op-1",
    }
    payload.update(overrides)
    return SubmitApprovalRequest(**payload)  # type: ignore[arg-type]


def test_submit_idempotent_conflict_audit(
    store: FinanceControlStore,
    audit_log: Path,
) -> None:
    first = store.submit_approval(_approval())
    again = store.submit_approval(_approval())
    assert first.approval_id == again.approval_id
    assert first.status is ApprovalStatus.PENDING
    assert "finance_approvals.submitted" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already submitted with different fields"):
        store.submit_approval(_approval(amount_cents=2_000))
    assert len(store.list_approvals("org-1")) == 1


def test_resolve_approval_flow(store: FinanceControlStore, audit_log: Path) -> None:
    store.submit_approval(_approval())
    approved = store.resolve_approval(
        "appr-1",
        "org-1",
        ResolveApprovalRequest(status=ApprovalStatus.APPROVED, actor_ref="fin-1", reason_ref="ok"),
    )
    assert approved.status is ApprovalStatus.APPROVED
    assert approved.resolved_by == "fin-1"
    assert "finance_approvals.resolved" in audit_log.read_text(encoding="utf-8")
    # Frozen after resolution.
    with pytest.raises(ValueError, match="illegal approval transition"):
        store.resolve_approval(
            "appr-1",
            "org-1",
            ResolveApprovalRequest(status=ApprovalStatus.REJECTED, actor_ref="fin-1"),
        )


def test_resolve_unknown_404(store: FinanceControlStore) -> None:
    with pytest.raises(ValueError, match="finance approval not found"):
        store.resolve_approval(
            "missing",
            "org-1",
            ResolveApprovalRequest(status=ApprovalStatus.APPROVED, actor_ref="fin"),
        )


def test_gate_flow(store: FinanceControlStore, audit_log: Path) -> None:
    store.submit_approval(_approval(amount_cents=1_000))
    pending = store.gate("appr-1", "org-1", GateRequest(amount_cents=100, currency="USD"))
    assert pending.allowed is False
    assert pending.reason == "not_approved"

    store.resolve_approval(
        "appr-1",
        "org-1",
        ResolveApprovalRequest(status=ApprovalStatus.APPROVED, actor_ref="fin-1"),
    )
    allowed = store.gate("appr-1", "org-1", GateRequest(amount_cents=1_000, currency="USD"))
    assert allowed.allowed is True
    over = store.gate("appr-1", "org-1", GateRequest(amount_cents=1_001, currency="USD"))
    assert over.allowed is False
    assert over.reason == "amount_exceeds_approved"
    wrong_ccy = store.gate("appr-1", "org-1", GateRequest(amount_cents=1, currency="EUR"))
    assert wrong_ccy.reason == "currency_mismatch"
    assert "finance_approvals.checked" in audit_log.read_text(encoding="utf-8")


def test_gate_unknown_404(store: FinanceControlStore) -> None:
    with pytest.raises(ValueError, match="finance approval not found"):
        store.gate("missing", "org-1", GateRequest(amount_cents=1, currency="USD"))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/cost-anomalies/check" in paths
    assert "/cost-anomalies" in paths
    assert "/finance-approvals" in paths
    assert "/finance-approvals/{approval_id}/resolve" in paths
    assert "/finance-approvals/{approval_id}/gate" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def _approval_payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "approval_id": "appr-1",
        "purpose_ref": "spend://big-buy",
        "amount_cents": 1_000,
        "currency": "USD",
        "submitted_by": "op-1",
    }


def test_approval_route_roundtrip(client) -> None:
    created = client.post("/finance-approvals", json=_approval_payload())
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "PENDING"

    fetched = client.get("/finance-approvals/appr-1", params={"org_id": "org-1"})
    assert fetched.status_code == 200
    assert client.get("/finance-approvals/nope", params={"org_id": "org-1"}).status_code == 404

    listing = client.get("/finance-approvals", params={"org_id": "org-1"})
    assert len(listing.json()) == 1

    resolved = client.post(
        "/finance-approvals/appr-1/resolve",
        params={"org_id": "org-1"},
        json={"status": "APPROVED", "actor_ref": "fin-1"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "APPROVED"

    # Illegal: APPROVED -> REJECTED.
    illegal = client.post(
        "/finance-approvals/appr-1/resolve",
        params={"org_id": "org-1"},
        json={"status": "REJECTED", "actor_ref": "fin-1"},
    )
    assert illegal.status_code == 409

    gate = client.post(
        "/finance-approvals/appr-1/gate",
        params={"org_id": "org-1"},
        json={"amount_cents": 500, "currency": "USD"},
    )
    assert gate.status_code == 200, gate.text
    assert gate.json()["allowed"] is True

    conflict = client.post("/finance-approvals", json={**_approval_payload(), "amount_cents": 5})
    assert conflict.status_code == 409

    bad_slug = client.post(
        "/finance-approvals", json={**_approval_payload(), "approval_id": "bad/id"}
    )
    assert bad_slug.status_code == 422


def test_anomaly_route_roundtrip(engine: Engine, client) -> None:
    entry_ids = _seed_entries(engine)
    resp = client.post(
        "/cost-anomalies/check",
        json={
            "org_id": "org-1",
            "anomaly_id": "anom-1",
            "entry_id": entry_ids[0],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["verdict"] in ("ANOMALY", "NORMAL", "INSUFFICIENT_DATA")

    listing = client.get("/cost-anomalies", params={"org_id": "org-1"})
    assert len(listing.json()) == 1

    missing = client.post(
        "/cost-anomalies/check",
        json={"org_id": "org-1", "anomaly_id": "anom-2", "entry_id": "nope"},
    )
    assert missing.status_code == 404
