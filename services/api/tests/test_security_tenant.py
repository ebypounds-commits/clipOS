"""Tenant-isolation and authorization regression tests (CLIP-092).

Verifies the 07_SECURITY_GOVERNANCE_RIGHTS.md server-side tenant-scoping
invariants against the real tenancy service and API stores:

* ``require_membership`` denies by default (unknown user/org, removed
  membership, suspended org) and never raises for absent access.
* The final active owner can never be removed or demoted (an org always
  keeps an owner), and that failure is a hard error, not a silent no-op.
* Cross-tenant reads never leak: every org-scoped store returns nothing
  for another org's keys, checked across the modules added in M14-M22.
* Capability-style gates fail closed on unknown or hostile inputs.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.tenancy import (
    MembershipRole,
    MembershipState,
    OrgState,
    TenancyService,
)

ORG_A = "org-a"
ORG_B = "org-b"


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-092-tenant-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def engine(audit_log: Path) -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    # Importing the app registers every module's Base metadata (the same
    # mechanism the running service uses), so create_all covers all tables.
    import clipos_api.main  # noqa: F401
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def tenancy() -> TenancyService:
    return TenancyService()


def _org_with_owner(tenancy: TenancyService, slug: str = "acme") -> tuple[str, str]:
    """Create an org and an ACTIVE owner; return (org_id, owner_user_id)."""
    org = tenancy.create_organization("Acme", slug)
    owner = f"{slug}-owner"
    tenancy.add_member(org.id, owner, MembershipRole.OWNER)
    return org.id, owner


# ---------------------------------------------------------------------------
# Deny-by-default tenant scoping
# ---------------------------------------------------------------------------


def test_require_membership_denies_unknown_user_or_org(tenancy: TenancyService) -> None:
    org_id, owner = _org_with_owner(tenancy)
    assert tenancy.require_membership(owner, org_id) is not None
    # Unknown user in a real org -> deny.
    assert tenancy.require_membership("stranger", org_id) is None
    # Known user in an unknown org -> deny.
    assert tenancy.require_membership(owner, "org-does-not-exist") is None


def test_require_membership_denies_suspended_org(tenancy: TenancyService) -> None:
    org_id, owner = _org_with_owner(tenancy)
    tenancy.set_org_state(org_id, OrgState.SUSPENDED)
    # Membership persists but access is refused while suspended.
    assert tenancy.require_membership(owner, org_id) is None
    tenancy.set_org_state(org_id, OrgState.ACTIVE)
    assert tenancy.require_membership(owner, org_id) is not None


def test_require_membership_denies_removed_member(tenancy: TenancyService) -> None:
    org_id, _owner = _org_with_owner(tenancy)
    tenancy.add_member(org_id, "member-1", MembershipRole.MEMBER)
    assert tenancy.require_membership("member-1", org_id) is not None
    tenancy.remove_member(org_id, "member-1")
    assert tenancy.require_membership("member-1", org_id) is None


def test_removed_member_state_is_not_active(tenancy: TenancyService) -> None:
    org_id, _owner = _org_with_owner(tenancy)
    tenancy.add_member(org_id, "member-1", MembershipRole.MEMBER)
    removed = tenancy.remove_member(org_id, "member-1")
    assert removed.state is not MembershipState.ACTIVE


# ---------------------------------------------------------------------------
# Owner invariant (privilege floor)
# ---------------------------------------------------------------------------


def test_final_owner_cannot_be_demoted(tenancy: TenancyService) -> None:
    org_id, owner = _org_with_owner(tenancy)
    with pytest.raises(ValueError):
        tenancy.set_member_role(org_id, owner, MembershipRole.MEMBER)
    assert tenancy.require_membership(owner, org_id) is not None


def test_final_owner_cannot_be_removed(tenancy: TenancyService) -> None:
    org_id, owner = _org_with_owner(tenancy)
    with pytest.raises(ValueError):
        tenancy.remove_member(org_id, owner)


def test_owner_can_be_demoted_once_another_owner_exists(
    tenancy: TenancyService,
) -> None:
    org_id, owner = _org_with_owner(tenancy)
    tenancy.add_member(org_id, "owner-2", MembershipRole.OWNER)
    demoted = tenancy.set_member_role(org_id, owner, MembershipRole.ADMIN)
    assert demoted.role is MembershipRole.ADMIN
    assert tenancy.require_membership("owner-2", org_id) is not None


# ---------------------------------------------------------------------------
# Cross-tenant isolation across the M14-M22 stores
# ---------------------------------------------------------------------------


def test_analytics_metrics_are_org_scoped(engine: Engine) -> None:
    from clipos_api.analytics import AnalyticsMetric, AnalyticsStore, MetricKind

    store = AnalyticsStore.from_engine(engine)
    store.store_metrics(
        ORG_A,
        "youtube",
        "content://shared-key",
        [AnalyticsMetric(kind=MetricKind.VIEW, value=1.0, unit="count")],
    )
    # Same content_ref under a different org must not be visible.
    assert store.get_metrics(ORG_B, "content://shared-key") == []
    assert len(store.get_metrics(ORG_A, "content://shared-key")) == 1


def test_budgets_are_org_scoped(engine: Engine) -> None:
    from clipos_api.budgets import BudgetStore, RegisterBudgetRequest

    store = BudgetStore.from_engine(engine)
    store.register_budget(
        RegisterBudgetRequest(
            org_id=ORG_A,
            budget_id="budget-1",
            scope_key="compute",
            amount_cents=1000,
            currency="USD",
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 7),
        )
    )
    # The same budget_id under another org is a different tenant space.
    assert store.get_budget("budget-1", ORG_B) is None
    assert store.get_budget("budget-1", ORG_A) is not None
    assert store.list_budgets(ORG_B) == []


def test_experiments_are_org_scoped(engine: Engine) -> None:
    from clipos_api.analytics import MetricKind
    from clipos_api.experiments import ExperimentStore, RegisterExperimentRequest

    store = ExperimentStore.from_engine(engine)
    store.register_experiment(
        RegisterExperimentRequest(
            org_id=ORG_A,
            experiment_id="exp-1",
            title="T",
            target_metric=MetricKind.VIEW,
            hypothesis_summary="H",
        )
    )
    assert store.get_experiment("exp-1", ORG_B) is None
    assert store.get_experiment("exp-1", ORG_A) is not None
    assert store.list_experiments(ORG_B) == []


def test_threat_model_is_org_scoped(engine: Engine) -> None:
    from clipos_api.threat_model import (
        RegisterThreatRequest,
        Severity,
        ThreatCategory,
        ThreatStore,
    )

    store = ThreatStore.from_engine(engine)
    store.register_threat(
        RegisterThreatRequest(
            org_id=ORG_A,
            threat_id="t-1",
            title="T",
            category=ThreatCategory.TAMPERING,
            severity=Severity.HIGH,
            abuse_case="abuse",
        )
    )
    assert store.get_threat("t-1", ORG_B) is None
    assert store.list_threats(ORG_B) == []
    assert store.coverage(ORG_B).total_threats == 0


def test_injection_events_are_org_scoped(engine: Engine) -> None:
    from clipos_api.injection_guard import EnforceRequest, InjectionStore

    store = InjectionStore.from_engine(engine)
    store.enforce(
        EnforceRequest(org_id=ORG_A, event_id="ev-1", source_ref="s", text="benign content")
    )
    assert store.list_events(ORG_B) == []
    assert len(store.list_events(ORG_A)) == 1


def test_platform_accounts_are_org_scoped(engine: Engine) -> None:
    from clipos_api.platform_accounts import (
        PlatformAccount,
        PlatformAccountStore,
    )

    store = PlatformAccountStore.from_engine(engine)
    store.register_account(
        PlatformAccount(
            account_id="acct-1",
            org_id=ORG_A,
            platform="youtube",
            account_ref="yt://a",
            display_name="A",
        )
    )
    assert store.get_account("acct-1", ORG_B) is None
    assert store.list_accounts(ORG_B) == []


def test_monitor_checks_are_org_scoped(engine: Engine) -> None:
    from clipos_api.monitors import MonitorKind, MonitorStore, RecordCheckRequest

    store = MonitorStore.from_engine(engine)
    store.record_check(
        RecordCheckRequest(
            org_id=ORG_A,
            check_id="chk-1",
            subsystem="quota",
            monitor_kind=MonitorKind.QUOTA_HEADROOM,
            subject_ref="quota://x",
            used=1.0,
            limit=100.0,
        )
    )
    assert store.list_checks(ORG_B) == []
    assert store.summarize(ORG_B).monitors == []


def test_incidents_are_org_scoped(engine: Engine) -> None:
    from clipos_api.incidents import IncidentStore, OpenIncidentRequest, Severity

    store = IncidentStore.from_engine(engine)
    store.open_incident(
        OpenIncidentRequest(
            org_id=ORG_A,
            incident_id="inc-1",
            title="T",
            subsystem="database",
            category="database",
            severity=Severity.SEV2,
            actor_ref="op",
        )
    )
    assert store.get_incident("inc-1", ORG_B) is None
    assert store.list_incidents(ORG_B) == []


# ---------------------------------------------------------------------------
# Fail-closed gates
# ---------------------------------------------------------------------------


def test_remediation_gate_denies_unknown_and_hostile_input() -> None:
    from clipos_api.remediation import RemediationAction, evaluate_remediation

    action = RemediationAction(
        action_id="a-1",
        org_id=ORG_A,
        action_kind="restart-service",
        command_template="svc restart {service}",
        max_attempts=2,
        requires_approval=True,
        enabled=True,
    )
    # Unknown parameter name -> deny (allow-list of placeholders).
    assert evaluate_remediation(action, {"other": "x"}, 0, approved=True).allowed is False
    # Injection attempt in a value -> deny.
    decision = evaluate_remediation(action, {"service": "api; rm -rf /"}, 0, approved=True)
    assert decision.allowed is False
    assert decision.reason == "params_invalid"
    # Kill switch wins over an otherwise-valid request.
    assert (
        evaluate_remediation(action, {"service": "api"}, 0, approved=True, kill_switch=True).reason
        == "kill_switch_active"
    )


def test_injection_boundary_fails_closed_on_scanner_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clipos_api.injection_guard as guard

    def boom(_text: str):
        raise RuntimeError("boom")

    monkeypatch.setattr(guard, "scan_untrusted_text", boom)
    result = guard.enforce_untrusted_boundary("content")
    assert result.decision is guard.EnforcementDecision.SCANNER_ERROR
    # Fail-closed: nothing is passed downstream on an error.
    assert result.safe_text == ""


def test_tool_authorization_denies_untrusted_justification() -> None:
    from clipos_api.injection_guard import (
        ToolAuthorizationRequest,
        authorize_tool_from_content,
    )

    decision = authorize_tool_from_content(
        ToolAuthorizationRequest(
            tool_id="clipos.shell",
            requested_by_ref="page://hostile",
            justification_source="RETRIEVED_CONTENT",
        )
    )
    assert decision.allowed is False
    assert decision.reason == "content_cannot_grant_tools"


def test_stock_adapter_rejects_blank_alias_and_query() -> None:
    """Provider-neutral adapters must not default their provider alias."""
    from pydantic import ValidationError

    from clipos_api.stock_adapter import StockSearchRequest

    with pytest.raises(ValidationError):
        StockSearchRequest(org_id=ORG_A, query="", provider_alias="p")
    with pytest.raises(ValidationError):
        StockSearchRequest(org_id=ORG_A, query="cats", provider_alias="")
