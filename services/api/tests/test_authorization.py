"""Authorization, role, and capability-policy tests (CLIP-008)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from clipos_api import audit
from clipos_api.authorization import (
    ADMIN_PERMISSIONS,
    AUDIT_READ,
    BUDGET_UPDATE,
    BUILTIN_ROLES,
    CONTENT_PUBLISH,
    CONTENT_REVIEW,
    GPU,
    KILLSWITCH_ACTIVATE,
    MEMBER_PERMISSIONS,
    MEMBER_ROLE_CHANGE,
    ORG_DELETE,
    ORG_READ,
    ORG_TRANSFER,
    OWNER_PERMISSIONS,
    REVIEWER_PERMISSIONS,
    SERVICE_AGENT_PERMISSIONS,
    AuthorizationService,
    CapabilityPolicy,
    Decision,
    Permission,
)
from clipos_api.tenancy import MembershipRole, TenancyService


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    return path


def events() -> list[dict[str, Any]]:
    return audit.read_events()


def tenancy() -> TenancyService:
    return TenancyService()


def grant(
    svc: TenancyService,
    *,
    user_id: str,
    role: MembershipRole,
) -> str:
    org = svc.create_organization(f"Org {user_id}", f"org-{user_id.replace('_', '-')}")
    svc.add_member(org.id, user_id, role)
    return org.id


def test_permission_accepts_valid_format() -> None:
    permission = Permission(value="org:read")
    assert permission.value == "org:read"
    assert Permission.model_validate("content:publish") == Permission(value="content:publish")


@pytest.mark.parametrize(
    "bad_value",
    ["", "Org:read", "org:Read", "orgread", "org:", ":read", "org:re ad", "org:read:extra"],
)
def test_permission_rejects_bad_format(bad_value: str) -> None:
    with pytest.raises(ValidationError):
        Permission(value=bad_value)


def test_builtin_role_hierarchy_and_special_permissions() -> None:
    owner = BUILTIN_ROLES["owner"].permissions
    assert owner == OWNER_PERMISSIONS
    assert BUILTIN_ROLES["admin"].permissions < owner
    assert BUILTIN_ROLES["member"].permissions < BUILTIN_ROLES["admin"].permissions
    assert BUILTIN_ROLES["reviewer"].permissions == REVIEWER_PERMISSIONS
    assert BUILTIN_ROLES["service_agent"].permissions == SERVICE_AGENT_PERMISSIONS
    assert KILLSWITCH_ACTIVATE in OWNER_PERMISSIONS
    assert KILLSWITCH_ACTIVATE not in BUILTIN_ROLES["admin"].permissions
    assert ORG_TRANSFER in OWNER_PERMISSIONS
    assert ORG_TRANSFER not in BUILTIN_ROLES["admin"].permissions
    assert ORG_DELETE not in BUILTIN_ROLES["admin"].permissions
    assert MEMBER_PERMISSIONS <= ADMIN_PERMISSIONS
    assert BUDGET_UPDATE in ADMIN_PERMISSIONS
    assert CONTENT_REVIEW in BUILTIN_ROLES["member"].permissions


@pytest.mark.parametrize(
    ("role", "permission", "expected"),
    [
        ("owner", ORG_READ, True),
        ("owner", ORG_DELETE, True),
        ("owner", ORG_TRANSFER, True),
        ("owner", KILLSWITCH_ACTIVATE, True),
        ("owner", BUDGET_UPDATE, True),
        ("admin", ORG_READ, True),
        ("admin", MEMBER_ROLE_CHANGE, True),
        ("admin", BUDGET_UPDATE, True),
        ("admin", ORG_TRANSFER, False),
        ("admin", ORG_DELETE, False),
        ("admin", KILLSWITCH_ACTIVATE, False),
        ("member", ORG_READ, True),
        ("member", Permission(value="settings:update"), True),
        ("member", CONTENT_REVIEW, True),
        ("member", AUDIT_READ, True),
        ("member", MEMBER_ROLE_CHANGE, False),
        ("member", BUDGET_UPDATE, False),
        ("member", CONTENT_PUBLISH, False),
        ("reviewer", AUDIT_READ, True),
        ("reviewer", CONTENT_REVIEW, True),
        ("reviewer", CONTENT_PUBLISH, True),
        ("reviewer", ORG_READ, False),
        ("reviewer", BUDGET_UPDATE, False),
        ("service_agent", CONTENT_REVIEW, True),
        ("service_agent", AUDIT_READ, False),
        ("service_agent", Permission(value="org:update"), False),
    ],
)
def test_authorize_role_permission_matrix(
    role: str,
    permission: Permission,
    expected: bool,
    audit_log: Path,
) -> None:
    @dataclass(frozen=True)
    class StubMembership:
        role: str

    class StubTenancy:
        def require_membership(self, user_id: str, org_id: str) -> StubMembership | None:
            return StubMembership(role=role)

    del audit_log
    service = AuthorizationService(StubTenancy())
    decision = service.authorize("user", "org", permission)
    assert decision.allowed is expected
    assert decision.permission == permission.value
    if expected:
        assert decision.reason == "granted"
        assert decision.role_used == role
    if expected or permission.value.endswith(":read"):
        assert events() == []  # allows and read-class denials are never audited
    else:
        assert [event["action"] for event in events()] == ["authz.denied"]


def test_non_member_is_denied(audit_log: Path) -> None:
    svc = tenancy()
    org_id = grant(svc, user_id="member-user", role=MembershipRole.MEMBER)
    service = AuthorizationService(svc)
    decision = service.authorize("stranger", org_id, ORG_READ)
    assert decision.allowed is False
    assert decision.reason == "no_active_membership"
    assert decision.role_used is None
    assert [event["action"] for event in events() if event["action"] == "authz.denied"] == []


def test_cross_tenant_membership_does_not_grant(audit_log: Path) -> None:
    svc = tenancy()
    org_a_id = grant(svc, user_id="cross-user", role=MembershipRole.ADMIN)
    org_b_id = grant(svc, user_id="other-user", role=MembershipRole.ADMIN)
    service = AuthorizationService(svc)
    allowed = service.authorize("cross-user", org_a_id, MEMBER_ROLE_CHANGE)
    denied = service.authorize("cross-user", org_b_id, MEMBER_ROLE_CHANGE)
    assert allowed.allowed is True
    assert allowed.role_used == "admin"
    assert denied.allowed is False
    assert denied.reason == "no_active_membership"
    denied_events = [event for event in events() if event["action"] == "authz.denied"]
    assert [event["action"] for event in denied_events] == ["authz.denied"]
    denied_event = denied_events[0]
    assert denied_event["actor"] == "cross-user"
    assert denied_event["subject"] == org_b_id
    assert denied_event["details"] == {
        "permission": "member:role_change",
        "reason": "no_active_membership",
        "role_used": None,
    }


def test_unknown_membership_role_is_denied(audit_log: Path) -> None:
    @dataclass(frozen=True)
    class StubMembership:
        role: str

    class StubTenancy:
        def require_membership(self, user_id: str, org_id: str) -> StubMembership | None:
            return StubMembership(role="superhero")

    del audit_log
    service = AuthorizationService(StubTenancy())
    decision = service.authorize("user", "org", ORG_READ)
    assert decision.allowed is False
    assert decision.reason == "unknown_role"
    assert decision.role_used == "superhero"


def test_denied_write_class_emits_one_audit_event(audit_log: Path) -> None:
    svc = tenancy()
    org_id = grant(svc, user_id="member-user", role=MembershipRole.MEMBER)
    service = AuthorizationService(svc)
    first = service.authorize("member-user", org_id, BUDGET_UPDATE)
    second = service.authorize("member-user", org_id, BUDGET_UPDATE)
    assert first == second
    assert first.allowed is False
    assert first.reason == "permission_not_granted"
    denial_events = [event for event in events() if event["action"] == "authz.denied"]
    assert len(denial_events) == 2
    assert denial_events[0]["details"]["permission"] == "budget:update"
    assert denial_events[0]["details"]["reason"] == "permission_not_granted"
    assert denial_events[0]["details"]["role_used"] == "member"


def test_denied_read_class_does_not_audit(audit_log: Path) -> None:
    svc = tenancy()
    org_id = grant(svc, user_id="member-user", role=MembershipRole.MEMBER)
    service = AuthorizationService(svc)
    decision = service.authorize("member-user", org_id, BUDGET_UPDATE)
    del decision
    read_denial = service.authorize("stranger", org_id, ORG_READ)
    assert read_denial.reason == "no_active_membership"
    write_events = [event for event in events() if event["details"].get("permission") == "org:read"]
    assert write_events == []


def test_authorize_is_deterministic_and_stateless(audit_log: Path) -> None:
    svc = tenancy()
    org_id = grant(svc, user_id="admin-user", role=MembershipRole.ADMIN)
    service = AuthorizationService(svc)
    first = service.authorize("admin-user", org_id, ORG_READ)
    second = service.authorize("admin-user", org_id, ORG_READ)
    assert (
        first
        == second
        == Decision(
            allowed=True,
            permission="org:read",
            reason="granted",
            role_used="admin",
        )
    )
    assert [event["action"] for event in events() if event["action"] == "authz.denied"] == []


def test_capability_policy_default_denies_gpu(audit_log: Path) -> None:
    del audit_log
    policy = CapabilityPolicy()
    assert policy.permit("render", [GPU]) is False
    assert policy.permit("search", [GPU, "network_egress"]) is False
    with pytest.raises(ValueError, match="tool_name"):
        policy.permit("", [])


def test_capability_policy_permits_exact_subsets_only(audit_log: Path) -> None:
    del audit_log
    policy = CapabilityPolicy(allowed=frozenset({GPU, "network_egress"}))
    assert policy.permit("render", []) is True
    assert policy.permit("render", [GPU]) is True
    assert policy.permit("render", [GPU, "network_egress"]) is True
    assert policy.permit("search", ["network_egress", "secrets_read"]) is False
    assert policy.permit("render", [GPU, GPU]) is True


def test_capability_denial_helper_records_event(audit_log: Path) -> None:
    policy = CapabilityPolicy()
    policy.audit_policy_denial("render", [GPU])
    denial = events()
    assert [event["action"] for event in denial] == ["capability.denied"]
    assert denial[0]["subject"] == "render"
    assert denial[0]["details"] == {"required": ["gpu"]}
