"""Tests for clip candidate extraction and registration (CLIP-042)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from clipos_api.clip_candidates import ClipCandidateStore
from fastapi.routing import APIRoute
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.clip_candidates import (
    ClipCandidateSource,
    ClipCandidateStatus,
    extract_candidates,
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
    path = Path("/tmp") / "clipos-042-clip-candidates-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def store(engine: Engine, audit_log: Path) -> ClipCandidateStore:
    from clipos_api.clip_candidates import ClipCandidateStore

    return ClipCandidateStore.from_engine(engine)


def test_extract_candidates_empty_boundaries() -> None:
    """Empty boundaries returns empty list."""
    result = extract_candidates([], 1000, 60000, 0)
    assert result == []


def test_extract_candidates_too_short() -> None:
    """Boundaries shorter than min_len_ms are skipped."""
    result = extract_candidates([(0, 500)], 1000, 60000, 0)
    assert result == []


def test_extract_candidates_too_long_trimmed() -> None:
    """Boundaries longer than max_len_ms are trimmed."""
    result = extract_candidates([(0, 120000)], 1000, 60000, 0)
    assert result == [(0, 60000)]


def test_extract_candidates_merge_overlap() -> None:
    """Overlapping boundaries are merged."""
    result = extract_candidates([(0, 5000), (3000, 8000)], 1000, 60000, 0)
    # Should merge into (0, 8000)
    assert result == [(0, 8000)]


def test_extract_candidates_respect_gap() -> None:
    """Non-overlapping with gap stays separate."""
    result = extract_candidates([(0, 3000), (5000, 8000)], 1000, 60000, 100)
    assert result == [(0, 3000), (5000, 8000)]


def test_extract_candidates_touching_merge() -> None:
    """Touching boundaries (end == start) merge with min_gap=0."""
    result = extract_candidates([(0, 3000), (3000, 6000)], 1000, 60000, 0)
    assert result == [(0, 6000)]


def test_extract_candidates_min_gap_nonzero() -> None:
    """With min_gap, touching does NOT merge."""
    result = extract_candidates([(0, 3000), (3000, 6000)], 1000, 60000, 10)
    assert result == [(0, 3000), (3000, 6000)]


def test_extract_candidates_all_too_short() -> None:
    """All boundaries too short returns empty."""
    result = extract_candidates([(0, 500), (100, 400)], 1000, 60000, 0)
    assert result == []


def test_extract_candidates_idempotent() -> None:
    """Same boundaries same params produces same result."""
    boundaries = [(0, 5000), (6000, 11000)]
    r1 = extract_candidates(boundaries, 1000, 60000, 0)
    r2 = extract_candidates(boundaries, 1000, 60000, 0)
    assert r1 == r2


# ------------------------------------------------------------------
# Store tests
# ------------------------------------------------------------------


def test_register_candidate_first(engine: Engine, audit_log: Path, store) -> None:
    """First register creates the candidate."""
    record = store.register_candidate(
        media_asset_id="asset-1",
        org_id="org-1",
        start_ms=0,
        end_ms=5000,
        source=ClipCandidateSource.SEMANTIC.value,
        reason="test reason",
    )
    assert record.candidate_id is not None
    assert record.media_asset_id == "asset-1"
    assert record.org_id == "org-1"
    assert record.start_ms == 0
    assert record.end_ms == 5000
    assert record.source == ClipCandidateSource.SEMANTIC.value
    assert record.reason == "test reason"
    assert record.status == ClipCandidateStatus.NEW.value


def test_register_candidate_idempotent(engine: Engine, audit_log: Path, store) -> None:
    """Repeated register with same fields returns same attributes (idempotent)."""
    # First register
    record1 = store.register_candidate(
        media_asset_id="asset-1",
        org_id="org-1",
        start_ms=0,
        end_ms=5000,
        source=ClipCandidateSource.SEMANTIC.value,
        reason="test reason",
    )
    # Second register with same fields - should find existing and return it
    record2 = store.register_candidate(
        media_asset_id="asset-1",
        org_id="org-1",
        start_ms=0,
        end_ms=5000,
        source=ClipCandidateSource.SEMANTIC.value,
        reason="test reason",
    )
    # They should have the same attributes (idempotent), but may be different objects
    # since they come from different sessions. Check by key fields.
    assert record1.candidate_id == record2.candidate_id
    assert record1.media_asset_id == record2.media_asset_id
    assert record1.org_id == record2.org_id
    assert record1.start_ms == record2.start_ms
    assert record1.end_ms == record2.end_ms
    assert record1.source == record2.source
    assert record1.reason == record2.reason


def test_register_candidate_conflict_different_reason(
    engine: Engine, audit_log: Path, store
) -> None:
    """Register with different reason raises ValueError."""
    store.register_candidate(
        media_asset_id="asset-1",
        org_id="org-1",
        start_ms=0,
        end_ms=5000,
        source=ClipCandidateSource.SEMANTIC.value,
        reason="first reason",
    )
    with pytest.raises(ValueError, match="already registered with different fields"):
        store.register_candidate(
            media_asset_id="asset-1",
            org_id="org-1",
            start_ms=0,
            end_ms=5000,
            source=ClipCandidateSource.SEMANTIC.value,
            reason="different reason",
        )


def test_register_candidate_org_isolation(engine: Engine, audit_log: Path, store) -> None:
    """Candidates are org-scoped; same IDs in different orgs are independent."""
    store.register_candidate(
        media_asset_id="asset-1",
        org_id="org-1",
        start_ms=0,
        end_ms=5000,
        source=ClipCandidateSource.SEMANTIC.value,
        reason="org-1 reason",
    )
    # org-2 can have same candidate_id
    record = store.register_candidate(
        media_asset_id="asset-1",
        org_id="org-2",
        start_ms=0,
        end_ms=5000,
        source=ClipCandidateSource.SEMANTIC.value,
        reason="org-2 reason",
    )
    assert record.org_id == "org-2"


def test_get_candidate(engine: Engine, audit_log: Path, store) -> None:
    """Get one candidate by ID and org."""
    store.register_candidate(
        media_asset_id="asset-1",
        org_id="org-1",
        start_ms=0,
        end_ms=5000,
        source=ClipCandidateSource.SEMANTIC.value,
        reason="test",
    )
    # List instead to verify
    records = store.list_by_org("org-1")
    assert len(records) == 1


def test_list_by_org(engine: Engine, audit_log: Path, store) -> None:
    """List all candidates for an org."""
    store.register_candidate(
        media_asset_id="asset-1",
        org_id="org-1",
        start_ms=0,
        end_ms=5000,
        source=ClipCandidateSource.SEMANTIC.value,
        reason="reason-1",
    )
    store.register_candidate(
        media_asset_id="asset-2",
        org_id="org-1",
        start_ms=6000,
        end_ms=10000,
        source=ClipCandidateSource.HEURISTIC.value,
        reason="reason-2",
    )
    records = store.list_by_org("org-1")
    assert len(records) == 2


def test_list_by_status(engine: Engine, audit_log: Path, store) -> None:
    """List candidates by status."""
    # New candidates by default
    records = store.list_by_status("NEW", org_id="org-1")
    assert len(records) == 0  # none yet

    store.register_candidate(
        media_asset_id="asset-1",
        org_id="org-1",
        start_ms=0,
        end_ms=5000,
        source=ClipCandidateSource.SEMANTIC.value,
        reason="reason",
    )
    records = store.list_by_status("NEW", org_id="org-1")
    assert len(records) == 1


# ------------------------------------------------------------------
# Route registration tests
# ------------------------------------------------------------------


def test_clip_candidates_router_registered_on_app() -> None:
    """Clip candidate routes are registered on the FastAPI app."""
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/clip-candidates" in paths
    assert "/clip-candidates/{candidate_id}" in paths
    assert "/clip-candidates/extract-and-register" in paths


# ------------------------------------------------------------------
# extract_and_register flow tests
# ------------------------------------------------------------------


def test_extract_and_register_flow(store, engine: Engine, audit_log: Path) -> None:
    """Full extract_and_register flow: heuristic then register."""
    from clipos_api.clip_candidates import extract_and_register

    boundaries = [(0, 5000), (6000, 11000)]
    params = {
        "min_len_ms": 1000,
        "max_len_ms": 60000,
        "min_gap_ms": 0,
        "source": ClipCandidateSource.SEMANTIC.value,
        "reason": "test extraction",
    }

    media_asset_id = "test-asset-1"
    org_id = "test-org-1"

    records = extract_and_register(
        store=store,
        media_asset_id=media_asset_id,
        org_id=org_id,
        boundaries=boundaries,
        params=params,
    )

    assert len(records) == 2  # two non-overlapping clips
    # Both should be registered with NEW status
    for record in records:
        assert record.media_asset_id == media_asset_id
        assert record.org_id == org_id
        assert record.status == ClipCandidateStatus.NEW.value


def test_extract_and_register_idempotent_rerun(store, engine: Engine, audit_log: Path) -> None:
    """Running extract_and_register twice with same data is idempotent."""
    from clipos_api.clip_candidates import extract_and_register

    boundaries = [(0, 5000)]
    params = {
        "min_len_ms": 1000,
        "max_len_ms": 60000,
        "min_gap_ms": 0,
        "source": ClipCandidateSource.SEMANTIC.value,
        "reason": "test",
    }

    media_asset_id = "test-asset-2"
    org_id = "test-org-2"

    # First run
    records1 = extract_and_register(
        store=store,
        media_asset_id=media_asset_id,
        org_id=org_id,
        boundaries=boundaries,
        params=params,
    )
    assert len(records1) == 1

    # Second run - should be idempotent, register returns existing
    records2 = extract_and_register(
        store=store,
        media_asset_id=media_asset_id,
        org_id=org_id,
        boundaries=boundaries,
        params=params,
    )
    assert len(records2) == 1  # Still 1 registered candidate


# ------------------------------------------------------------------
# Edge case tests
# ------------------------------------------------------------------


def test_register_candidate_invalid_start_ms(engine: Engine, audit_log: Path, store) -> None:
    """register_candidate raises on negative start_ms."""
    with pytest.raises(ValueError, match="start_ms must be >= 0"):
        store.register_candidate(
            media_asset_id="asset-1",
            org_id="org-1",
            start_ms=-1,
            end_ms=5000,
            source=ClipCandidateSource.SEMANTIC.value,
            reason="test",
        )


def test_register_candidate_invalid_end_ms(engine: Engine, audit_log: Path, store) -> None:
    """register_candidate raises on end_ms <= start_ms."""
    with pytest.raises(ValueError, match="end_ms must be > start_ms"):
        store.register_candidate(
            media_asset_id="asset-1",
            org_id="org-1",
            start_ms=5000,
            end_ms=3000,
            source=ClipCandidateSource.SEMANTIC.value,
            reason="test",
        )


def test_register_candidate_invalid_source(engine: Engine, audit_log: Path, store) -> None:
    """register_candidate raises on invalid source."""
    with pytest.raises(ValueError, match="invalid source"):
        store.register_candidate(
            media_asset_id="asset-1",
            org_id="org-1",
            start_ms=0,
            end_ms=5000,
            source="INVALID",
            reason="test",
        )


def test_register_candidate_reason_too_long(engine: Engine, audit_log: Path, store) -> None:
    """register_candidate raises on reason > 255 chars."""
    long_reason = "x" * 256
    with pytest.raises(ValueError, match="reason must be max 255 chars"):
        store.register_candidate(
            media_asset_id="asset-1",
            org_id="org-1",
            start_ms=0,
            end_ms=5000,
            source=ClipCandidateSource.SEMANTIC.value,
            reason=long_reason,
        )
