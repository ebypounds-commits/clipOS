"""Tests for brand lifecycle and shutdown policies (CLIP-081)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.brand_lifecycle import (
    BrandLifecycleStatus,
    BrandLifecycleStore,
    LifecycleTransitionRequest,
    RegisterLifecycleRequest,
    RegisterShutdownPolicyRequest,
    ShutdownAction,
    lifecycle_transition_allowed,
)
from clipos_api.brand_templates import (
    BrandTemplateStore,
    RegisterBrandTemplateRequest,
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
    path = Path("/tmp") / "clipos-081-lifecycle-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> BrandLifecycleStore:
    return BrandLifecycleStore.from_engine(engine)


def _seed_brand(engine: Engine) -> None:
    BrandTemplateStore.from_engine(engine).register_brand(
        RegisterBrandTemplateRequest(org_id="org-1", template_id="brand-1", title="Main")
    )


def _lifecycle(**overrides: object) -> RegisterLifecycleRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "lifecycle_id": "lc-1",
        "brand_template_id": "brand-1",
    }
    payload.update(overrides)
    return RegisterLifecycleRequest(**payload)  # type: ignore[arg-type]


def _shutdown(**overrides: object) -> RegisterShutdownPolicyRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "policy_id": "sd-1",
        "brand_template_id": "brand-1",
        "action": ShutdownAction.KEEP_PUBLISHED,
        "grace_days": 30,
    }
    payload.update(overrides)
    return RegisterShutdownPolicyRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure lifecycle
# ---------------------------------------------------------------------------


def test_lifecycle_paths() -> None:
    assert lifecycle_transition_allowed(BrandLifecycleStatus.DRAFT, BrandLifecycleStatus.ACTIVE)
    assert lifecycle_transition_allowed(BrandLifecycleStatus.ACTIVE, BrandLifecycleStatus.SUSPENDED)
    assert lifecycle_transition_allowed(BrandLifecycleStatus.SUSPENDED, BrandLifecycleStatus.ACTIVE)
    assert lifecycle_transition_allowed(
        BrandLifecycleStatus.SUSPENDED, BrandLifecycleStatus.RETIRED
    )
    assert not lifecycle_transition_allowed(
        BrandLifecycleStatus.DRAFT, BrandLifecycleStatus.RETIRED
    )
    assert not lifecycle_transition_allowed(
        BrandLifecycleStatus.RETIRED, BrandLifecycleStatus.ACTIVE
    )
    assert lifecycle_transition_allowed(BrandLifecycleStatus.ACTIVE, BrandLifecycleStatus.ACTIVE)


# ---------------------------------------------------------------------------
# Store: lifecycles
# ---------------------------------------------------------------------------


def test_lifecycle_requires_brand(store: BrandLifecycleStore) -> None:
    with pytest.raises(ValueError, match="brand template not found"):
        store.register_lifecycle(_lifecycle())


def test_lifecycle_one_per_brand(
    store: BrandLifecycleStore,
    engine: Engine,
    audit_log: Path,
) -> None:
    _seed_brand(engine)
    first = store.register_lifecycle(_lifecycle())
    again = store.register_lifecycle(_lifecycle())
    assert first.lifecycle_id == again.lifecycle_id
    assert first.status is BrandLifecycleStatus.DRAFT
    assert "brand_lifecycles.registered" in audit_log.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="already registered for brand"):
        store.register_lifecycle(_lifecycle(lifecycle_id="lc-2"))
    # Same lifecycle_id pointed at a different (existing) brand -> conflict.
    from clipos_api.brand_templates import RegisterBrandTemplateRequest as RB

    BrandTemplateStore.from_engine(engine).register_brand(
        RB(org_id="org-1", template_id="brand-2", title="Second")
    )
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_lifecycle(_lifecycle(brand_template_id="brand-2"))


def test_lifecycle_transition_flow(
    store: BrandLifecycleStore,
    engine: Engine,
    audit_log: Path,
) -> None:
    _seed_brand(engine)
    store.register_lifecycle(_lifecycle())
    store.transition_lifecycle(
        "lc-1",
        "org-1",
        LifecycleTransitionRequest(status=BrandLifecycleStatus.ACTIVE, actor_ref="op"),
    )
    store.transition_lifecycle(
        "lc-1",
        "org-1",
        LifecycleTransitionRequest(
            status=BrandLifecycleStatus.SUSPENDED, actor_ref="op", reason_ref="policy"
        ),
    )
    resumed = store.transition_lifecycle(
        "lc-1",
        "org-1",
        LifecycleTransitionRequest(status=BrandLifecycleStatus.ACTIVE, actor_ref="op"),
    )
    assert resumed.status is BrandLifecycleStatus.ACTIVE
    retired = store.transition_lifecycle(
        "lc-1",
        "org-1",
        LifecycleTransitionRequest(status=BrandLifecycleStatus.SUSPENDED, actor_ref="op"),
    )
    assert retired.status is BrandLifecycleStatus.SUSPENDED
    final = store.transition_lifecycle(
        "lc-1",
        "org-1",
        LifecycleTransitionRequest(status=BrandLifecycleStatus.RETIRED, actor_ref="op"),
    )
    assert final.status is BrandLifecycleStatus.RETIRED
    assert "brand_lifecycles.status_changed" in audit_log.read_text(encoding="utf-8")


def test_lifecycle_illegal_and_unknown(
    store: BrandLifecycleStore,
    engine: Engine,
) -> None:
    _seed_brand(engine)
    store.register_lifecycle(_lifecycle())
    with pytest.raises(ValueError, match="illegal lifecycle transition"):
        store.transition_lifecycle(
            "lc-1",
            "org-1",
            LifecycleTransitionRequest(status=BrandLifecycleStatus.RETIRED, actor_ref="op"),
        )
    with pytest.raises(ValueError, match="brand lifecycle not found"):
        store.transition_lifecycle(
            "missing",
            "org-1",
            LifecycleTransitionRequest(status=BrandLifecycleStatus.ACTIVE, actor_ref="op"),
        )


def test_list_lifecycle_status_filter(
    store: BrandLifecycleStore,
    engine: Engine,
) -> None:
    _seed_brand(engine)
    from clipos_api.brand_templates import RegisterBrandTemplateRequest as RB

    BrandTemplateStore.from_engine(engine).register_brand(
        RB(org_id="org-1", template_id="brand-2", title="Second")
    )
    store.register_lifecycle(_lifecycle())
    store.register_lifecycle(_lifecycle(lifecycle_id="lc-2", brand_template_id="brand-2"))
    store.transition_lifecycle(
        "lc-2",
        "org-1",
        LifecycleTransitionRequest(status=BrandLifecycleStatus.ACTIVE, actor_ref="op"),
    )
    assert len(store.list_lifecycles("org-1")) == 2
    assert len(store.list_lifecycles("org-1", BrandLifecycleStatus.DRAFT)) == 1
    assert len(store.list_lifecycles("org-1", BrandLifecycleStatus.ACTIVE)) == 1
    assert len(store.list_lifecycles("org-other")) == 0


# ---------------------------------------------------------------------------
# Store: shutdown policies
# ---------------------------------------------------------------------------


def test_shutdown_requires_brand(store: BrandLifecycleStore) -> None:
    with pytest.raises(ValueError, match="brand template not found"):
        store.register_shutdown(_shutdown())


def test_shutdown_one_per_brand_idempotent(
    store: BrandLifecycleStore,
    engine: Engine,
    audit_log: Path,
) -> None:
    _seed_brand(engine)
    first = store.register_shutdown(_shutdown())
    again = store.register_shutdown(_shutdown())
    assert first.policy_id == again.policy_id
    assert first.action is ShutdownAction.KEEP_PUBLISHED
    assert first.grace_days == 30
    assert "shutdown_policies.registered" in audit_log.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="already registered for brand"):
        store.register_shutdown(_shutdown(policy_id="sd-2"))
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_shutdown(_shutdown(action=ShutdownAction.UNLIST_PUBLISHED))
    assert len(store.list_shutdowns("org-1")) == 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/brand-lifecycles" in paths
    assert "/brand-lifecycles/{lifecycle_id}/transition" in paths
    assert "/shutdown-policies" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_full_route_roundtrip(client) -> None:
    brand = client.post(
        "/brand-templates",
        json={"org_id": "org-1", "template_id": "brand-1", "title": "Main"},
    )
    assert brand.status_code == 200, brand.text

    created = client.post(
        "/brand-lifecycles",
        json={
            "org_id": "org-1",
            "lifecycle_id": "lc-1",
            "brand_template_id": "brand-1",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "DRAFT"

    listing = client.get("/brand-lifecycles", params={"org_id": "org-1"})
    assert len(listing.json()) == 1

    # DRAFT -> ACTIVE -> SUSPENDED -> RETIRED
    for status in ("ACTIVE", "SUSPENDED", "RETIRED"):
        resp = client.post(
            "/brand-lifecycles/lc-1/transition",
            params={"org_id": "org-1"},
            json={"status": status, "actor_ref": "op"},
        )
        assert resp.status_code == 200, (status, resp.text)

    illegal = client.post(
        "/brand-lifecycles/lc-1/transition",
        params={"org_id": "org-1"},
        json={"status": "ACTIVE", "actor_ref": "op"},
    )
    assert illegal.status_code == 409

    policy = client.post(
        "/shutdown-policies",
        json={
            "org_id": "org-1",
            "policy_id": "sd-1",
            "brand_template_id": "brand-1",
            "action": "SCHEDULE_DELETE",
            "grace_days": 7,
        },
    )
    assert policy.status_code == 200, policy.text
    assert policy.json()["grace_days"] == 7

    policies = client.get("/shutdown-policies", params={"org_id": "org-1"})
    assert len(policies.json()) == 1


def test_route_errors(client) -> None:
    orphan = client.post(
        "/brand-lifecycles",
        json={
            "org_id": "org-1",
            "lifecycle_id": "lc-x",
            "brand_template_id": "missing",
        },
    )
    assert orphan.status_code == 404

    missing_transition = client.post(
        "/brand-lifecycles/nope/transition",
        params={"org_id": "org-1"},
        json={"status": "ACTIVE", "actor_ref": "op"},
    )
    assert missing_transition.status_code == 404

    bad_grace = client.post(
        "/shutdown-policies",
        json={
            "org_id": "org-1",
            "policy_id": "sd-bad",
            "brand_template_id": "brand-1",
            "action": "KEEP_PUBLISHED",
            "grace_days": 365,
        },
    )
    assert bad_grace.status_code == 422

    bad_slug = client.post(
        "/brand-lifecycles",
        json={
            "org_id": "org-1",
            "lifecycle_id": "bad/id",
            "brand_template_id": "brand-1",
        },
    )
    assert bad_slug.status_code == 422
