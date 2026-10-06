"""Typed workflow conventions mandated by 06_WORKFLOW_ORCHESTRATION.md.

This module is pure configuration and logic: metadata carrying, business-key
formats, retry categories, durable timers, human approvals, and audit hooks.
It performs no I/O except through the shared additive audit module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from temporalio import workflow
from temporalio.common import RetryPolicy

from clipos_api import audit

_POLICY_VERSION_PATTERN = r"^v\d+(?:\.[0-9]+)*$"

# Business-key formats exactly as specified in 06_WORKFLOW_ORCHESTRATION.md.
_BUSINESS_KEY_PARTS: dict[str, tuple[str, ...]] = {
    "publish": ("account_id", "variant_id", "scheduled_slot"),
    "render": ("project_id", "render_version"),
    "rights-review": ("source_item_id", "rights_version"),
    "analytics": ("publication_id", "window"),
}


class WorkflowMetadata(BaseModel):
    """Workflow-carried identity, policy, and correlation context.

    Every workflow carries organization, brand/content identifiers, policy
    version, and correlation ID; ``as_dict`` is search-attribute-friendly.
    """

    model_config = ConfigDict(frozen=True)

    organization_id: str = Field(min_length=1)
    brand_id: str | None = Field(default=None, min_length=1)
    content_id: str | None = Field(default=None, min_length=1)
    policy_version: str = Field(pattern=_POLICY_VERSION_PATTERN)
    correlation_id: str = Field(min_length=1)

    def as_dict(self) -> dict[str, str | None]:
        """Return a plain mapping safe to carry as workflow input data."""
        return self.model_dump(mode="json")


def business_key(kind: str, **parts: str) -> str:
    """Build the 06 idempotency business key for ``kind``.

    Missing or extra parts raise ``ValueError``; every required part must be a
    non-empty string.
    """
    required = _BUSINESS_KEY_PARTS.get(kind)
    if required is None:
        valid = ", ".join(sorted(_BUSINESS_KEY_PARTS))
        raise ValueError(f"unknown business key kind {kind!r}; valid kinds: {valid}")
    missing = [name for name in required if name not in parts]
    extra = [name for name in parts if name not in required]
    if missing:
        raise ValueError(f"business key {kind!r} missing parts: {missing}")
    if extra:
        raise ValueError(f"business key {kind!r} got unexpected parts: {extra}")
    empty = [name for name in required if not str(parts[name]).strip()]
    if empty:
        raise ValueError(f"business key {kind!r} parts must be non-empty: {empty}")
    return ":".join((kind, *(str(parts[name]) for name in required)))


class RetryCategory(str, Enum):
    """The exact retry categories from 06_WORKFLOW_ORCHESTRATION.md."""

    NETWORK_TRANSIENT = "network_transient"
    RATE_LIMIT = "rate_limit"
    AUTH_EXPIRED = "auth_expired"
    INVALID_REQUEST = "invalid_request"
    POLICY_REJECTION = "policy_rejection"
    MEDIA_CORRUPTION = "media_corruption"
    INSUFFICIENT_RIGHTS = "insufficient_rights"


def retry_policy_for(category: RetryCategory) -> RetryPolicy:
    """Map a retry category to a Temporal retry policy.

    Invalid request and policy rejection never retry blindly; auth expiry gets
    exactly one refresh attempt; media corruption and insufficient rights are
    terminal for the activity and surfaced for quarantine/operator handling.
    """
    match category:
        case RetryCategory.NETWORK_TRANSIENT:
            return RetryPolicy(
                initial_interval=timedelta(seconds=2),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=60),
                maximum_attempts=10,
            )
        case RetryCategory.RATE_LIMIT:
            return RetryPolicy(
                initial_interval=timedelta(seconds=30),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=300),
                maximum_attempts=5,
            )
        case RetryCategory.AUTH_EXPIRED:
            return RetryPolicy(
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=1.0,
                maximum_attempts=2,
            )
        case RetryCategory.INVALID_REQUEST:
            return RetryPolicy(
                maximum_attempts=1,
                non_retryable_error_types=[RetryCategory.INVALID_REQUEST.value],
            )
        case RetryCategory.POLICY_REJECTION:
            return RetryPolicy(
                maximum_attempts=1,
                non_retryable_error_types=[RetryCategory.POLICY_REJECTION.value],
            )
        case RetryCategory.MEDIA_CORRUPTION:
            return RetryPolicy(
                maximum_attempts=1,
                non_retryable_error_types=[RetryCategory.MEDIA_CORRUPTION.value],
            )
        case RetryCategory.INSUFFICIENT_RIGHTS:
            return RetryPolicy(
                maximum_attempts=1,
                non_retryable_error_types=[RetryCategory.INSUFFICIENT_RIGHTS.value],
            )


async def durable_wait(seconds: float) -> None:
    """Sleep on a durable timer for ``seconds``.

    MUST be called only inside a workflow context (deterministic replay); it
    delegates to ``temporalio.workflow.sleep``. Never call ``asyncio.sleep``
    from workflow code.
    """
    if seconds < 0:
        raise ValueError("durable_wait seconds must be non-negative")
    await workflow.sleep(seconds)


class ApprovalRequest(BaseModel):
    """Human approval request with the exact fields from 06.

    The default timeout behavior is deny; expiry must be a tz-aware UTC
    datetime in the future at creation time.
    """

    model_config = ConfigDict(frozen=True)

    action: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    expiry: datetime
    approver_role: str = Field(min_length=1)
    timeout_behavior: Literal["deny"] = "deny"

    @field_validator("evidence")
    @classmethod
    def _evidence_non_empty(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("evidence entries must be non-empty")
        return value

    @field_validator("expiry")
    @classmethod
    def _expiry_is_future_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expiry must be timezone-aware")
        normalized = value.astimezone(UTC)
        if normalized <= datetime.now(UTC):
            raise ValueError("expiry must be in the future")
        return normalized


def approval_expired(request: ApprovalRequest, now: datetime) -> bool:
    """Return whether the approval window has passed at ``now`` (deny on timeout)."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return request.expiry <= now.astimezone(UTC)


def audit_workflow_event(name: str, correlation_id: str, details: dict[str, Any]) -> dict[str, Any]:
    """Record a structured, operator-visible workflow state-change event."""
    if not name or not correlation_id:
        raise ValueError("workflow event name and correlation_id are required")
    return audit.record(
        action=f"workflow.{name}",
        actor="workflow-worker",
        subject=correlation_id,
        details=details,
    )
