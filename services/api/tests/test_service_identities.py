"""Service identity lifecycle tests (CLIP-009)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from clipos_api import audit
from clipos_api.service_identities import (
    ServiceIdentityKind,
    ServiceIdentityService,
    ServiceIdentityState,
)


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    return path


def now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def events() -> list[dict[str, Any]]:
    return audit.read_events()


def service() -> ServiceIdentityService:
    return ServiceIdentityService(clock=now)


def test_create_idempotent_one_event(audit_log: Path) -> None:
    svc = service()
    identity1, raw1 = svc.create("my-agent", ServiceIdentityKind.SERVICE_AGENT)
    identity2, raw2 = svc.create("my-agent", ServiceIdentityKind.SERVICE_AGENT)
    assert identity1.id == identity2.id
    assert raw1 is not None
    assert raw2 is None
    created = [e for e in events() if e["action"] == "service_identity.created"]
    assert len(created) == 1


def test_raw_token_not_in_any_stored_field(audit_log: Path) -> None:
    svc = service()
    identity, raw = svc.create("my-agent", ServiceIdentityKind.SERVICE_AGENT)
    assert raw is not None
    serialized = json.dumps(identity.model_dump(mode="json"), default=str)
    assert raw not in serialized
    assert identity.token_hash is not None
    assert identity.token_hash != raw
    assert len(identity.token_hash) == 64


def test_credential_ref_stored_verbatim_never_secret(audit_log: Path) -> None:
    svc = service()
    ref = "vault:prod/clipos-identity"
    identity, raw = svc.create("my-agent", ServiceIdentityKind.SERVICE_AGENT, credential_ref=ref)
    assert raw is not None
    assert identity.credential_ref == ref
    assert identity.credential_ref != raw


def test_rotation_invalidates_old_token(audit_log: Path) -> None:
    svc = service()
    _, raw1 = svc.create("my-agent", ServiceIdentityKind.SERVICE_AGENT)
    assert raw1 is not None
    assert svc.authenticate(raw1) is not None
    _, raw2 = svc.issue_token("my-agent")
    assert svc.authenticate(raw1) is None
    assert svc.authenticate(raw2) is not None


def test_disable_blocks_authenticate_idempotent(audit_log: Path) -> None:
    svc = service()
    _, raw = svc.create("my-agent", ServiceIdentityKind.SERVICE_AGENT)
    assert raw is not None
    svc.disable("my-agent")
    assert svc.authenticate(raw) is None
    disabled_before = [e for e in events() if e["action"] == "service_identity.disabled"]
    svc.disable("my-agent")
    disabled_after = [e for e in events() if e["action"] == "service_identity.disabled"]
    assert len(disabled_after) == len(disabled_before)


def test_enable_restores(audit_log: Path) -> None:
    svc = service()
    _, raw = svc.create("my-agent", ServiceIdentityKind.SERVICE_AGENT)
    assert raw is not None
    svc.disable("my-agent")
    assert svc.authenticate(raw) is None
    svc.enable("my-agent")
    assert svc.authenticate(raw) is not None


def test_authenticate_unknown_returns_none(audit_log: Path) -> None:
    svc = service()
    assert svc.authenticate("unknown-token-value") is None


def test_concurrent_same_name_create_single_identity(audit_log: Path) -> None:
    svc = service()
    barrier = threading.Barrier(8)
    results: list[tuple[str, str | None]] = []

    def worker() -> None:
        barrier.wait()
        identity, raw = svc.create("shared-agent", ServiceIdentityKind.SERVICE_AGENT)
        results.append((identity.name, raw))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    assert len({name for name, _ in results}) == 1
    raw_tokens = [raw for _, raw in results if raw is not None]
    assert len(raw_tokens) == 1
    created = [e for e in events() if e["action"] == "service_identity.created"]
    assert len(created) == 1


def test_audit_event_sequence(audit_log: Path) -> None:
    svc = service()
    svc.create("my-agent", ServiceIdentityKind.SERVICE_AGENT)
    svc.issue_token("my-agent")
    svc.disable("my-agent")
    svc.enable("my-agent")
    actions = [e["action"] for e in events() if e["action"].startswith("service_identity")]
    assert actions == [
        "service_identity.created",
        "service_identity.token_rotated",
        "service_identity.disabled",
        "service_identity.enabled",
    ]


def test_get_and_list(audit_log: Path) -> None:
    svc = service()
    svc.create("agent-a", ServiceIdentityKind.SERVICE_AGENT, org_id="org-1")
    svc.create("agent-b", ServiceIdentityKind.WORKER, org_id="org-2")
    svc.create("agent-c", ServiceIdentityKind.INTEGRATION)
    assert svc.get("agent-a") is not None
    assert svc.get("missing") is None
    assert len(svc.list()) == 3
    assert len(svc.list(org_id="org-1")) == 1
    assert len(svc.list(org_id="org-2")) == 1
    assert len(svc.list()) == 3


def test_invalid_name_rejected(audit_log: Path) -> None:
    svc = service()
    with pytest.raises(ValidationError):
        svc.create("INVALID NAME", ServiceIdentityKind.SERVICE_AGENT)


def test_invalid_credential_ref_rejected(audit_log: Path) -> None:
    svc = service()
    with pytest.raises(ValidationError, match="reference"):
        svc.create("my-agent", ServiceIdentityKind.SERVICE_AGENT, credential_ref="not-a-reference")


def test_issue_token_unknown_name_raises(audit_log: Path) -> None:
    svc = service()
    with pytest.raises(ValueError, match="not found"):
        svc.issue_token("nonexistent")


def test_disable_unknown_name_raises(audit_log: Path) -> None:
    svc = service()
    with pytest.raises(ValueError, match="not found"):
        svc.disable("nonexistent")


def test_enable_unknown_name_raises(audit_log: Path) -> None:
    svc = service()
    with pytest.raises(ValueError, match="not found"):
        svc.enable("nonexistent")


def test_disabled_at_set_and_cleared(audit_log: Path) -> None:
    svc = service()
    svc.create("my-agent", ServiceIdentityKind.SERVICE_AGENT)
    identity = svc.disable("my-agent")
    assert identity.state == ServiceIdentityState.DISABLED
    assert identity.disabled_at == now()
    identity = svc.enable("my-agent")
    assert identity.state == ServiceIdentityState.ACTIVE
    assert identity.disabled_at is None
