import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from clipos_api import audit
from clipos_api.workflow_framework import (
    ApprovalRequest,
    RetryCategory,
    WorkflowMetadata,
    approval_expired,
    audit_workflow_event,
    business_key,
    durable_wait,
    retry_policy_for,
)


def _metadata() -> WorkflowMetadata:
    return WorkflowMetadata(
        organization_id="org-1",
        brand_id="brand-1",
        content_id="content-1",
        policy_version="v1",
        correlation_id="corr-1",
    )


def test_metadata_valid_and_as_dict() -> None:
    metadata = _metadata()
    assert metadata.as_dict() == {
        "organization_id": "org-1",
        "brand_id": "brand-1",
        "content_id": "content-1",
        "policy_version": "v1",
        "correlation_id": "corr-1",
    }
    with pytest.raises(ValidationError):
        metadata.correlation_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"organization_id": ""},
        {"policy_version": "latest"},
        {"policy_version": ""},
        {"correlation_id": ""},
        {"brand_id": ""},
        {"content_id": ""},
    ],
)
def test_metadata_validation_errors(kwargs: dict[str, str]) -> None:
    values: dict[str, str] = {
        "organization_id": "org-1",
        "brand_id": "brand-1",
        "content_id": "content-1",
        "policy_version": "v1.2.0",
        "correlation_id": "corr-1",
    }
    values.update(kwargs)
    with pytest.raises(ValidationError):
        WorkflowMetadata(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kind", "parts", "expected"),
    [
        (
            "publish",
            {"account_id": "a1", "variant_id": "v1", "scheduled_slot": "slot"},
            "publish:a1:v1:slot",
        ),
        (
            "render",
            {"project_id": "p1", "render_version": "r1"},
            "render:p1:r1",
        ),
        (
            "rights-review",
            {"source_item_id": "s1", "rights_version": "rv1"},
            "rights-review:s1:rv1",
        ),
        (
            "analytics",
            {"publication_id": "pub1", "window": "24h"},
            "analytics:pub1:24h",
        ),
    ],
)
def test_business_key_exact_formats(kind: str, parts: dict[str, str], expected: str) -> None:
    assert business_key(kind, **parts) == expected


def test_business_key_missing_extra_unknown() -> None:
    with pytest.raises(ValueError, match="missing"):
        business_key("publish", account_id="a1", variant_id="v1")
    with pytest.raises(ValueError, match="unexpected parts"):
        business_key("render", project_id="p1", render_version="r1", extra="x")
    with pytest.raises(ValueError, match="unknown business key kind"):
        business_key("unknown", x="1")


def test_retry_categories_and_policies() -> None:
    assert {category.value for category in RetryCategory} == {
        "network_transient",
        "rate_limit",
        "auth_expired",
        "invalid_request",
        "policy_rejection",
        "media_corruption",
        "insufficient_rights",
    }
    transient = retry_policy_for(RetryCategory.NETWORK_TRANSIENT)
    assert transient.maximum_attempts >= 10
    assert transient.backoff_coefficient >= 2.0
    assert transient.initial_interval >= timedelta(seconds=2)
    rate = retry_policy_for(RetryCategory.RATE_LIMIT)
    assert rate.maximum_attempts >= 5
    assert rate.initial_interval >= timedelta(seconds=30)
    assert retry_policy_for(RetryCategory.AUTH_EXPIRED).maximum_attempts == 2
    for category in (
        RetryCategory.INVALID_REQUEST,
        RetryCategory.POLICY_REJECTION,
        RetryCategory.MEDIA_CORRUPTION,
        RetryCategory.INSUFFICIENT_RIGHTS,
    ):
        assert retry_policy_for(category).maximum_attempts == 1


def test_durable_wait_is_documented_wrapper_only() -> None:
    assert durable_wait.__doc__ is not None
    assert "workflow context" in durable_wait.__doc__
    import asyncio

    with pytest.raises(Exception, match="[Nn]ot in workflow"):
        asyncio.run(durable_wait(0.01))


def _approval(expiry: datetime) -> ApprovalRequest:
    return ApprovalRequest(
        action="publish.first_public_post",
        risk="first public post is externally visible",
        evidence=["rights gate passed", "policy gate passed"],
        expiry=expiry,
        approver_role="operator",
    )


def test_approval_request_validation() -> None:
    request = _approval(datetime.now(UTC) + timedelta(minutes=5))
    assert request.timeout_behavior == "deny"
    with pytest.raises(ValidationError):
        _approval(datetime.now(UTC) - timedelta(minutes=5))
    with pytest.raises(ValidationError):
        ApprovalRequest(
            action="a",
            risk="r",
            evidence=[""],
            expiry=datetime.now(UTC) + timedelta(minutes=5),
            approver_role="operator",
        )
    naive = _approval(datetime.now(UTC) + timedelta(minutes=5)).expiry.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        ApprovalRequest(
            action="a",
            risk="r",
            evidence=["e"],
            expiry=naive,
            approver_role="operator",
        )


def test_approval_expired_true_false() -> None:
    request = _approval(datetime.now(UTC) + timedelta(minutes=5))
    assert not approval_expired(request, datetime.now(UTC))
    assert approval_expired(request, request.expiry)
    assert approval_expired(request, request.expiry + timedelta(seconds=1))


def test_audit_workflow_event_parseable_json(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(audit, "AUDIT_PATH", tmp_path / "audit.log")
    event = audit_workflow_event("approval_requested", "corr-1", {"outcome": "approved"})
    parsed = json.loads((tmp_path / "audit.log").read_text().strip())
    assert parsed["action"] == "workflow.approval_requested"
    assert parsed["actor"] == "workflow-worker"
    assert parsed["subject"] == "corr-1"
    assert parsed["details"] == {"outcome": "approved"}
    assert event == parsed
    with pytest.raises(ValueError):
        audit_workflow_event("", "corr-1", {})
