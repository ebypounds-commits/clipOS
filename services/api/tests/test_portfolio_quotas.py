"""Tests for portfolio allocation/quota engine (CLIP-080)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.main import app
from clipos_api.portfolio_quotas import (
    QuotaScopeKind,
    QuotaStore,
    QuotaWindow,
    RecordAllocationRequest,
    RegisterQuotaRequest,
    allocate_portfolio,
    evaluate_quota,
)


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-080-quota-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine) -> QuotaStore:
    return QuotaStore.from_engine(engine)


def _quota(**overrides: object) -> RegisterQuotaRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "quota_id": "q-1",
        "scope_kind": QuotaScopeKind.ACCOUNT,
        "scope_ref": "acct://yt-1",
        "window": QuotaWindow.DAILY,
        "max_count": 5,
    }
    payload.update(overrides)
    return RegisterQuotaRequest(**payload)  # type: ignore[arg-type]


def _allocation(**overrides: object) -> RecordAllocationRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "allocation_id": "alloc-1",
        "total_units": 100,
        "weights": {"short-form": 60, "long-form": 40},
    }
    payload.update(overrides)
    return RecordAllocationRequest(**payload)  # type: ignore[arg-type]


def _seed_jobs(engine: Engine, account_ref: str = "acct://yt-1", count: int = 3) -> None:
    """Insert recent publication_jobs rows for quota counting."""
    from clipos_api.publishing import PublicationJobRecord

    now = datetime.now(UTC)
    with engine.begin() as conn:
        for i in range(count):
            conn.execute(
                PublicationJobRecord.__table__.insert().values(
                    job_id=f"job-{i}",
                    org_id="org-1",
                    platform="youtube",
                    account_ref=account_ref,
                    content_ref=f"content://{i}",
                    title=f"t{i}",
                    visibility="PRIVATE",
                    operating_mode="PRIVATE_TEST",
                    idempotency_key=f"idem-{i}",
                    status="SUCCEEDED",
                    decision_reason="allowed",
                    external_id=f"ext-{i}",
                    error_ref=None,
                    created_at=now,
                )
            )


# ---------------------------------------------------------------------------
# Pure allocation
# ---------------------------------------------------------------------------


def test_allocate_sums_exactly() -> None:
    result = allocate_portfolio(100, {"a": 1, "b": 1, "c": 1})
    assert sum(result.values()) == 100
    assert set(result) == {"a", "b", "c"}


def test_allocate_weighted_split() -> None:
    result = allocate_portfolio(100, {"heavy": 60, "light": 40})
    assert result["heavy"] == 60
    assert result["light"] == 40


def test_allocate_largest_remainder_tie_deterministic() -> None:
    # 10 units / 3 equal weights -> 3,3,4 (remainder 1 goes to first key).
    first = allocate_portfolio(10, {"a": 1, "b": 1, "c": 1})
    second = allocate_portfolio(10, {"a": 1, "b": 1, "c": 1})
    assert first == second
    assert sum(first.values()) == 10
    assert sorted(first.values()) == [3, 3, 4]


def test_allocate_single_channel() -> None:
    assert allocate_portfolio(7, {"only": 5}) == {"only": 7}


def test_allocate_zero_units() -> None:
    assert allocate_portfolio(0, {"a": 1, "b": 2}) == {"a": 0, "b": 0}


def test_allocate_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="total_units"):
        allocate_portfolio(-1, {"a": 1})
    with pytest.raises(ValueError, match="empty"):
        allocate_portfolio(10, {})
    with pytest.raises(ValueError, match="positive value"):
        allocate_portfolio(10, {"a": 0, "b": 0})
    with pytest.raises(ValueError, match=">= 0"):
        allocate_portfolio(10, {"a": -1})


# ---------------------------------------------------------------------------
# Pure quota evaluation
# ---------------------------------------------------------------------------


def test_quota_within_and_boundary() -> None:
    inside = evaluate_quota(4, 5, 1)
    assert inside.allowed is True
    assert inside.reason == "within_quota"
    assert inside.used_count + inside.proposed_count == inside.max_count
    partial = evaluate_quota(3, 5, 2)
    assert partial.allowed is True


def test_quota_exceeds() -> None:
    outside = evaluate_quota(5, 5, 1)
    assert outside.allowed is False
    assert outside.reason == "exceeds_quota"


# ---------------------------------------------------------------------------
# Store: quotas
# ---------------------------------------------------------------------------


def test_quota_register_idempotent_conflict_audit(
    store: QuotaStore,
    audit_log: Path,
) -> None:
    first = store.register_quota(_quota())
    again = store.register_quota(_quota())
    assert first.quota_id == again.quota_id
    assert "quota_policies.registered" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_quota(_quota(max_count=9))
    assert len(store.list_quotas("org-1")) == 1


def test_quota_check_counts_publication_jobs(
    store: QuotaStore,
    engine: Engine,
    audit_log: Path,
) -> None:
    store.register_quota(_quota(max_count=10))
    _seed_jobs(engine, count=3)

    result = store.check_quota("q-1", "org-1", proposed_count=1)
    assert result.allowed is True
    assert result.used_count == 3
    assert result.max_count == 10
    assert "quota_checks.evaluated" in audit_log.read_text(encoding="utf-8")

    # Scope filter: other account's jobs are not counted.
    other = store.register_quota(_quota(quota_id="q-other", scope_ref="acct://other"))
    assert other.quota_id == "q-other"
    other_result = store.check_quota("q-other", "org-1", proposed_count=1)
    assert other_result.used_count == 0

    # Exceeds: request more than remaining.
    store.register_quota(_quota(quota_id="q-tight", max_count=3))
    tight = store.check_quota("q-tight", "org-1", proposed_count=1)
    assert tight.allowed is False
    assert tight.reason == "exceeds_quota"


def test_quota_check_unknown_404(store: QuotaStore) -> None:
    with pytest.raises(ValueError, match="quota policy not found"):
        store.check_quota("missing", "org-1", proposed_count=1)


def test_quota_check_window_excludes_old_jobs(
    store: QuotaStore,
    engine: Engine,
) -> None:
    from clipos_api.publishing import PublicationJobRecord

    store.register_quota(_quota(max_count=10))
    old = datetime.now(UTC) - timedelta(days=3)
    with engine.begin() as conn:
        conn.execute(
            PublicationJobRecord.__table__.insert().values(
                job_id="job-old",
                org_id="org-1",
                platform="youtube",
                account_ref="acct://yt-1",
                content_ref="content://old",
                title="old",
                visibility="PRIVATE",
                operating_mode="PRIVATE_TEST",
                idempotency_key="idem-old",
                status="SUCCEEDED",
                decision_reason="allowed",
                external_id="ext-old",
                error_ref=None,
                created_at=old,
            )
        )
    result = store.check_quota("q-1", "org-1", proposed_count=1)
    assert result.used_count == 0


# ---------------------------------------------------------------------------
# Store: allocations
# ---------------------------------------------------------------------------


def test_allocation_record_idempotent_conflict(
    store: QuotaStore,
    audit_log: Path,
) -> None:
    first = store.record_allocation(_allocation())
    again = store.record_allocation(_allocation())
    assert first.allocation_id == again.allocation_id
    assert sum(first.allocation.values()) == 100
    assert "portfolio_allocations.recorded" in audit_log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already recorded with different fields"):
        store.record_allocation(_allocation(total_units=50))
    assert len(store.list_allocations("org-1")) == 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/quota-policies" in paths
    assert "/quota-policies/{quota_id}/check" in paths
    assert "/portfolio-allocations" in paths


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_quota_route_roundtrip(engine: Engine, client) -> None:
    created = client.post(
        "/quota-policies",
        json={
            "org_id": "org-1",
            "quota_id": "q-1",
            "scope_kind": "ACCOUNT",
            "scope_ref": "acct://yt-1",
            "window": "DAILY",
            "max_count": 5,
        },
    )
    assert created.status_code == 200, created.text

    listing = client.get("/quota-policies", params={"org_id": "org-1"})
    assert len(listing.json()) == 1

    conflict = client.post(
        "/quota-policies",
        json={
            "org_id": "org-1",
            "quota_id": "q-1",
            "scope_kind": "ACCOUNT",
            "scope_ref": "acct://yt-1",
            "window": "DAILY",
            "max_count": 9,
        },
    )
    assert conflict.status_code == 409

    _seed_jobs(engine, count=4)
    check = client.post(
        "/quota-policies/q-1/check",
        params={"org_id": "org-1"},
        json={"proposed_count": 1},
    )
    assert check.status_code == 200, check.text
    assert check.json()["used_count"] == 4
    assert check.json()["allowed"] is True

    missing = client.post(
        "/quota-policies/nope/check",
        params={"org_id": "org-1"},
        json={"proposed_count": 1},
    )
    assert missing.status_code == 404


def test_allocation_route_roundtrip(client) -> None:
    created = client.post(
        "/portfolio-allocations",
        json={
            "org_id": "org-1",
            "allocation_id": "alloc-1",
            "total_units": 100,
            "weights": {"short-form": 60, "long-form": 40},
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert sum(body["allocation"].values()) == 100

    listing = client.get("/portfolio-allocations", params={"org_id": "org-1"})
    assert len(listing.json()) == 1

    conflict = client.post(
        "/portfolio-allocations",
        json={
            "org_id": "org-1",
            "allocation_id": "alloc-1",
            "total_units": 50,
            "weights": {"short-form": 60, "long-form": 40},
        },
    )
    assert conflict.status_code == 409

    bad_weights = client.post(
        "/portfolio-allocations",
        json={
            "org_id": "org-1",
            "allocation_id": "alloc-2",
            "total_units": 10,
            "weights": {},
        },
    )
    assert bad_weights.status_code == 422

    bad_slug = client.post(
        "/portfolio-allocations",
        json={
            "org_id": "org-1",
            "allocation_id": "bad/id",
            "total_units": 10,
            "weights": {"a": 1},
        },
    )
    assert bad_slug.status_code == 422
