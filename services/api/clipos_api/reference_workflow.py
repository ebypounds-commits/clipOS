"""Reference workflow proving the CLIP-015 framework conventions.

Deterministic-friendly reference only: activities keep state in an injected
in-memory store (no database, no external side effects). The store is scoped
to the activity instance; tests pass one shared instance so idempotency across
workflow executions on the same worker is provable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import activity, workflow

from clipos_api.workflow_framework import (
    ApprovalRequest,
    RetryCategory,
    WorkflowMetadata,
    audit_workflow_event,
    durable_wait,
    retry_policy_for,
)


@dataclass(frozen=True)
class FrameworkSelfTestInput:
    """Serializable workflow input; ``metadata`` validates as WorkflowMetadata."""

    metadata: dict[str, Any]
    business_key: str
    wait_seconds: float = 1.0
    approve_after_seconds: float = 0.05
    approval_ttl_seconds: float = 60.0


_idempotency_store: dict[str, int] = {}


def use_idempotency_store(store: dict[str, int]) -> None:
    """Point the reference activity at ``store`` (worker-bootstrap-time only)."""
    global _idempotency_store
    _idempotency_store = store


@activity.defn
async def record_idempotent_step(key: str) -> dict[str, bool]:
    """Record the first occurrence of ``key`` in the injected store."""
    first_run = key not in _idempotency_store
    _idempotency_store[key] = _idempotency_store.get(key, 0) + 1
    return {"first_run": first_run}


@activity.defn
async def evaluate_approval(args: tuple[str, float, str]) -> dict[str, Any]:
    """Create the 06 approval request, then audit request and decision."""
    correlation_id, ttl_seconds, outcome = args
    request = ApprovalRequest(
        action="reference.self_test",
        risk="reference workflow in-memory only; no external side effects",
        evidence=[f"correlation_id={correlation_id}"],
        expiry=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        approver_role="operator",
    )
    audit_workflow_event(
        "approval_requested",
        correlation_id,
        {"action": request.action, "timeout_behavior": request.timeout_behavior},
    )
    audit_workflow_event(
        "self_test_completed",
        correlation_id,
        {"outcome": outcome, "timeout_behavior": request.timeout_behavior},
    )
    return {"approved": True}


@workflow.defn
class FrameworkSelfTestWorkflow:
    """Run an idempotent step, wait on durable timers, then decide approval."""

    @workflow.run
    async def run(self, data: FrameworkSelfTestInput) -> dict[str, Any]:
        metadata = WorkflowMetadata.model_validate(data.metadata)
        first_run_result: dict[str, bool] = await workflow.execute_activity(
            record_idempotent_step,
            data.business_key,
            start_to_close_timeout=timedelta(seconds=5),
            retry_policy=retry_policy_for(RetryCategory.NETWORK_TRANSIENT),
        )
        await durable_wait(data.wait_seconds)
        approval_wait = min(data.approval_ttl_seconds, data.approve_after_seconds)
        await durable_wait(approval_wait)
        approved = data.approve_after_seconds <= data.approval_ttl_seconds
        outcome = "approved" if approved else "denied_timeout"
        await workflow.execute_activity(
            evaluate_approval,
            (metadata.correlation_id, data.approval_ttl_seconds, outcome),
            start_to_close_timeout=timedelta(seconds=5),
            retry_policy=retry_policy_for(RetryCategory.NETWORK_TRANSIENT),
        )
        return {
            "outcome": outcome,
            "correlation_id": metadata.correlation_id,
            "first_run": first_run_result["first_run"],
        }
