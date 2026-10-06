"""Tests for organization tenancy and memberships (CLIP-007)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from clipos_api import audit
from clipos_api.tenancy import (
    InMemoryMembershipStore,
    InMemoryOrganizationStore,
    MembershipRole,
    MembershipState,
    Organization,
    OrgState,
    TenancyService,
)


class Clock:
    """Injectable test clock: no sleeps."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta):
        self.now += delta


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict]:
    path = tmp_path / "audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    return path


def actions() -> list[str]:
    return [event["action"] for event in audit.read_events()]


def service(audit_log: list[dict]) -> TenancyService:
    return TenancyService(clock=Clock())


def test_slug_format_rejects_bad_input(audit_log: list[dict]) -> None:
    svc = service(audit_log)
    for bad_slug in ("", "A-upper", "-leading", "ends-", "a", "a" * 64, "has space", "under_score"):
        with pytest.raises(ValueError):
            svc.create_organization("Org", bad_slug)
    with pytest.raises(ValidationError):
        Organization(id="id1", name="Org", slug="BAD", created_at=Clock()(), updated_at=Clock()())
    assert actions() == []


def test_create_organization_is_idempotent_on_slug(audit_log: list[dict]) -> None:
    svc = service(audit_log)
    first = svc.create_organization("Acme", "acme", idempotency="key-1")
    second = svc.create_organization("Acme 2", "acme", idempotency="key-2")
    assert first.id == second.id
    assert first.name == "Acme"  # unchanged on replay
    assert actions().count("org.created") == 1
    assert svc.get_organization(first.id) == first
    assert svc.slug_available("acme") is False
    assert svc.slug_available("acme-2") is True


def test_add_member_is_idempotent(audit_log: list[dict]) -> None:
    svc = service(audit_log)
    org = svc.create_organization("Acme", "acme")
    first = svc.add_member(org.id, "user-1", MembershipRole.ADMIN)
    second = svc.add_member(org.id, "user-1", MembershipRole.MEMBER)
    assert first.id == second.id
    assert second.role == MembershipRole.ADMIN  # unchanged on replay
    assert actions().count("member.added") == 1


def test_membership_lifecycle_with_audit_sequence(audit_log: list[dict]) -> None:
    svc = service(audit_log)
    org = svc.create_organization("Acme", "acme")
    svc.add_member(org.id, "owner-1", MembershipRole.OWNER)
    svc.add_member(org.id, "member-1", MembershipRole.MEMBER)
    svc.set_member_role(org.id, "member-1", MembershipRole.ADMIN)
    svc.remove_member(org.id, "member-1")
    svc.remove_member(org.id, "member-1")  # idempotent, no duplicate audit
    members = svc.members_of_org(org.id, active_only=False)
    assert len(members) == 2
    removed = next(m for m in members if m.user_id == "member-1")
    assert removed.state == MembershipState.REMOVED
    assert actions() == [
        "org.created",
        "member.added",
        "member.added",
        "member.role_changed",
        "member.removed",
    ]


def test_last_owner_protection(audit_log: list[dict]) -> None:
    svc = service(audit_log)
    org = svc.create_organization("Acme", "acme")
    svc.add_member(org.id, "owner-1", MembershipRole.OWNER)
    with pytest.raises(ValueError, match="final active owner"):
        svc.remove_member(org.id, "owner-1")
    with pytest.raises(ValueError, match="final active owner"):
        svc.set_member_role(org.id, "owner-1", MembershipRole.MEMBER)
    svc.add_member(org.id, "owner-2", MembershipRole.OWNER)
    demoted = svc.set_member_role(org.id, "owner-1", MembershipRole.ADMIN)
    assert demoted.role == MembershipRole.ADMIN
    assert len(svc.members_of_org(org.id)) == 2
    assert actions().count("member.removed") == 0


def test_require_membership_tenant_scoping(audit_log: list[dict]) -> None:
    svc = service(audit_log)
    org = svc.create_organization("Acme", "acme")
    other = svc.create_organization("Beta", "beta")
    svc.add_member(org.id, "user-1", MembershipRole.MEMBER)
    assert svc.require_membership("user-1", org.id) is not None
    assert svc.require_membership("user-1", other.id) is None  # non-member of other org
    assert svc.require_membership("user-2", org.id) is None
    svc.remove_member(org.id, "user-1")
    assert svc.require_membership("user-1", org.id) is None  # removed member denied


def test_membership_listing_filters(audit_log: list[dict]) -> None:
    svc = service(audit_log)
    org = svc.create_organization("Acme", "acme")
    other = svc.create_organization("Beta", "beta")
    svc.add_member(org.id, "owner-1", MembershipRole.OWNER)
    svc.add_member(org.id, "user-1", MembershipRole.MEMBER)
    svc.add_member(other.id, "user-1", MembershipRole.MEMBER)
    svc.remove_member(org.id, "user-1")
    assert [m.user_id for m in svc.members_of_org(org.id)] == ["owner-1"]
    assert len(svc.members_of_org(org.id, active_only=False)) == 2
    assert [o.slug for o in svc.orgs_of_user("user-1")] == ["beta"]
    assert [o.slug for o in svc.orgs_of_user("owner-1")] == ["acme"]


def test_suspended_org_keeps_memberships_but_denies_access(audit_log: list[dict]) -> None:
    svc = service(audit_log)
    org = svc.create_organization("Acme", "acme")
    svc.add_member(org.id, "user-1", MembershipRole.MEMBER)
    svc.set_org_state(org.id, OrgState.SUSPENDED)
    assert svc.members_of_org(org.id)  # memberships persist; reads still work
    assert svc.require_membership("user-1", org.id) is None
    svc.set_org_state(org.id, OrgState.ACTIVE)
    assert svc.require_membership("user-1", org.id) is not None
    assert actions() == ["org.created", "member.added", "org.suspended", "org.reactivated"]


def test_concurrent_same_slug_creates_exactly_one_org(audit_log: list[dict]) -> None:
    svc = service(audit_log)
    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def run() -> None:
        barrier.wait()
        org = svc.create_organization("Acme", "acme")
        with lock:
            results.append(org.id)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(results)) == 1
    assert actions().count("org.created") == 1


def test_concurrent_distinct_slugs_create_n_orgs(audit_log: list[dict]) -> None:
    svc = service(audit_log)
    barrier = threading.Barrier(8)
    org_ids: list[str] = []
    lock = threading.Lock()

    def run(index: int) -> None:
        barrier.wait()
        org = svc.create_organization(f"Org {index}", f"org-{index}")
        with lock:
            org_ids.append(org.id)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(org_ids)) == 8
    assert actions().count("org.created") == 8


def test_store_protocols_and_thread_safety_types(audit_log: list[dict]) -> None:
    svc = service(audit_log)
    assert isinstance(svc._orgs, InMemoryOrganizationStore)
    assert isinstance(svc._memberships, InMemoryMembershipStore)
    org = svc.create_organization("Acme", "acme")
    svc.add_member(org.id, "user-1")
    with pytest.raises(ValueError, match="not found"):
        svc.add_member("missing-org", "user-1")
    with pytest.raises(ValueError, match="not found"):
        svc.remove_member(org.id, "missing-user")
