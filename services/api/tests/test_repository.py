"""SQLite-backed tests for the repository base and SQL stores (CLIP-010)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from clipos_api import repository, tenancy

_NOW = datetime.now(UTC)


def engine() -> Engine:
    """Create a fresh private in-memory SQLite engine with the schema applied."""
    sqlite_engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    repository.Base.metadata.create_all(sqlite_engine)
    return sqlite_engine


def make_org(
    org_id: str = "org-1",
    slug: str = "acme",
    name: str = "Acme",
) -> tenancy.Organization:
    return tenancy.Organization(
        id=org_id,
        name=name,
        slug=slug,
        created_at=_NOW,
        updated_at=_NOW,
    )


def make_membership(org_id: str, user_id: str, role: tenancy.MembershipRole) -> tenancy.Membership:
    return tenancy.Membership(
        id=f"mem-{org_id}-{user_id}",
        org_id=org_id,
        user_id=user_id,
        role=role,
        joined_at=_NOW,
        updated_at=_NOW,
    )


def test_organization_repository_roundtrip() -> None:
    repo = repository.OrganizationRepository(engine())
    record = repo.add(
        repository.OrganizationRecord(
            id="org-1",
            name="Acme",
            slug="acme",
            state="active",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    assert repo.get("org-1") is not None
    assert repo.get("missing") is None
    assert len(repo.list()) == 1
    assert [record.id for record in repo.list(limit=1, offset=0)] == ["org-1"]
    assert repo.list(limit=1, offset=1) == []
    assert record.slug == "acme"
    assert repo.delete("org-1") is True
    assert repo.delete("org-1") is False
    assert repo.list() == []


def test_slug_uniqueness_surfaces_as_error() -> None:
    repo = repository.OrganizationRepository(engine())
    repo.add(
        repository.OrganizationRecord(
            id="org-1",
            name="Acme",
            slug="acme",
            state="active",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    with pytest.raises(IntegrityError):
        repo.add(
            repository.OrganizationRecord(
                id="org-2",
                name="Other",
                slug="acme",
                state="active",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


def test_membership_uniqueness_surfaces_as_error() -> None:
    repo = repository.MembershipRepository(engine())
    repo.add(
        repository.MembershipRecord(
            id="mem-1",
            org_id="org-1",
            user_id="user-1",
            role="member",
            state="active",
            joined_at=_NOW,
            updated_at=_NOW,
        )
    )
    with pytest.raises(IntegrityError):
        repo.add(
            repository.MembershipRecord(
                id="mem-2",
                org_id="org-1",
                user_id="user-1",
                role="member",
                state="active",
                joined_at=_NOW,
                updated_at=_NOW,
            )
        )


@pytest.fixture(params=["memory", "sql"], ids=["in-memory", "sql"])
def org_store(
    request: pytest.FixtureRequest,
) -> tenancy.InMemoryOrganizationStore | repository.SQLOrganizationStore:
    if request.param == "memory":
        return tenancy.InMemoryOrganizationStore()
    return repository.SQLOrganizationStore(engine())


def test_organization_store_conformance(
    org_store: tenancy.InMemoryOrganizationStore | repository.SQLOrganizationStore,
) -> None:
    assert isinstance(org_store, tenancy.OrganizationStore)
    assert org_store.get("org-1") is None
    assert org_store.get_by_slug("acme") is None
    first = org_store.put_if_absent_by_slug(make_org())
    replay = org_store.put_if_absent_by_slug(make_org(org_id="org-2", name="Acme 2"))
    assert first.id == replay.id == "org-1"
    assert org_store.get("org-1") == first
    assert org_store.get_by_slug("acme") == first
    updated = first.model_copy(update={"name": "Acme Co", "updated_at": _NOW})
    org_store.update(updated)
    assert org_store.get("org-1") == updated
    assert org_store.get_by_slug("acme") == updated


@pytest.fixture(params=["memory", "sql"], ids=["in-memory", "sql"])
def membership_store(
    request: pytest.FixtureRequest,
) -> tenancy.InMemoryMembershipStore | repository.SQLMembershipStore:
    if request.param == "memory":
        return tenancy.InMemoryMembershipStore()
    return repository.SQLMembershipStore(engine())


def test_membership_store_conformance(
    membership_store: tenancy.InMemoryMembershipStore | repository.SQLMembershipStore,
) -> None:
    assert isinstance(membership_store, tenancy.MembershipStore)
    assert membership_store.get("org-1", "user-1") is None
    first = membership_store.put_if_absent(
        make_membership("org-1", "user-1", tenancy.MembershipRole.ADMIN)
    )
    replay = membership_store.put_if_absent(
        make_membership("org-1", "user-1", tenancy.MembershipRole.MEMBER)
    )
    assert first.id == replay.id
    assert replay.role == tenancy.MembershipRole.ADMIN  # unchanged on replay
    membership_store.update(
        replay.model_copy(
            update={
                "state": tenancy.MembershipState.REMOVED,
                "updated_at": _NOW,
            }
        )
    )
    assert membership_store.get("org-1", "user-1").state == tenancy.MembershipState.REMOVED  # type: ignore[union-attr]
    other = membership_store.put_if_absent(
        make_membership("org-2", "user-1", tenancy.MembershipRole.MEMBER)
    )
    assert [m.org_id for m in membership_store.list_by_org("org-1")] == ["org-1"]
    assert {m.org_id for m in membership_store.list_by_user("user-1")} == {"org-1", "org-2"}
    assert other.org_id == "org-2"


def test_tenancy_service_runs_on_sql_stores() -> None:
    test_engine = engine()
    svc = tenancy.TenancyService(
        org_store=repository.SQLOrganizationStore(test_engine),
        membership_store=repository.SQLMembershipStore(test_engine),
    )
    org = svc.create_organization("Acme", "acme")
    member = svc.add_member(org.id, "user-1", tenancy.MembershipRole.OWNER)
    assert svc.get_organization(org.id) == org
    assert svc.require_membership("user-1", org.id) == member
