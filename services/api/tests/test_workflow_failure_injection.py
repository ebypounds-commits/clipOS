"""Live Temporal retry-injection and business-key search-attribute tests.

These tests require the local dev Temporal frontend; they skip honestly when
``TEMPORAL_ADDRESS`` is absent. Orchestrator and CI run them against the stack.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.client import RPCError
from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes

from clipos_api.reference_workflow import (
    FrameworkSelfTestInput,
    FrameworkSelfTestWorkflow,
    evaluate_approval,
    record_idempotent_step,
    use_idempotency_store,
)
from clipos_api.worker import WorkerConfig, connect_client, run_worker_until_stopped
from clipos_api.workflow_framework import RetryCategory, business_key, retry_policy_for

_BUSINESS_KEY = SearchAttributeKey.for_keyword("CustomKeywordField")


@pytest.mark.skipif(not os.environ.get("TEMPORAL_ADDRESS"), reason="live Temporal not configured")
def test_transient_activity_fails_once_then_workflow_retries_to_success() -> None:
    asyncio.run(_run_transient_retry())


@pytest.mark.skipif(not os.environ.get("TEMPORAL_ADDRESS"), reason="live Temporal not configured")
def test_workflow_is_findable_by_typed_business_key_search_attribute() -> None:
    asyncio.run(_run_business_key_visibility())


@dataclass(frozen=True)
class FlakyActivityInput:
    """Attempt counter key; a unique nonce isolates each live test execution."""

    key: str


_retry_attempts: dict[str, int] = {}


def use_retry_attempts(store: dict[str, int]) -> None:
    """Point the flaky activity at a worker-bootstrap test store."""
    global _retry_attempts
    _retry_attempts = store


@activity.defn
async def fail_first_then_succeed(data: FlakyActivityInput) -> dict[str, int]:
    """Raise on the first attempt for a key, then record and return attempts."""
    _retry_attempts[data.key] = _retry_attempts.get(data.key, 0) + 1
    if _retry_attempts[data.key] == 1:
        raise RuntimeError("injected transient failure")
    return {"attempts": _retry_attempts[data.key]}


@workflow.defn
class FlakyRetryWorkflow:
    """Run one transiently failing activity under the CLIP-015 retry policy."""

    @workflow.run
    async def run(self, data: FlakyActivityInput) -> dict[str, int]:
        return await workflow.execute_activity(
            fail_first_then_succeed,
            data,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy_for(RetryCategory.NETWORK_TRANSIENT),
        )


async def _start_worker(workflows: list[type], activities: list[object]) -> tuple[object, object]:
    config = WorkerConfig.from_env()
    client = await connect_client(config)
    worker_task = asyncio.create_task(
        run_worker_until_stopped(client, config, workflows, activities)
    )
    return client, worker_task


async def _run_transient_retry() -> None:
    nonce = uuid.uuid4().hex
    key = f"flaky-{nonce}"
    attempts: dict[str, int] = {}
    use_retry_attempts(attempts)
    client, worker_task = await _start_worker([FlakyRetryWorkflow], [fail_first_then_succeed])
    handle = None
    try:
        handle = await client.start_workflow(
            FlakyRetryWorkflow.run,
            FlakyActivityInput(key=key),
            id=f"flaky-retry-{nonce}",
            task_queue=WorkerConfig.from_env().task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result == {"attempts": 2}
        assert attempts[key] == 2
    finally:
        if handle is not None:
            with contextlib.suppress(RPCError):
                await handle.terminate()  # already-completed executions cannot be terminated
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task


async def _run_business_key_visibility() -> None:
    store: dict[str, int] = {}
    use_idempotency_store(store)
    nonce = uuid.uuid4().hex
    key = business_key("render", project_id=f"search-{nonce}", render_version="1")
    metadata = {
        "organization_id": f"org-{nonce}",
        "policy_version": "v1",
        "correlation_id": f"search-{nonce}",
    }
    client, worker_task = await _start_worker(
        [FrameworkSelfTestWorkflow], [record_idempotent_step, evaluate_approval]
    )
    handle = None
    try:
        handle = await client.start_workflow(
            FrameworkSelfTestWorkflow.run,
            FrameworkSelfTestInput(metadata=metadata, business_key=key),
            id=f"business-key-search-{nonce}",
            task_queue=WorkerConfig.from_env().task_queue,
            execution_timeout=timedelta(seconds=30),
            search_attributes=TypedSearchAttributes([SearchAttributePair(_BUSINESS_KEY, key)]),
        )
        await asyncio.wait_for(handle.result(), timeout=60)
        query = f"CustomKeywordField = '{key}'"
        matches = [execution async for execution in client.list_workflows(query=query, limit=10)]
        assert any(execution.id == handle.id for execution in matches)
    finally:
        if handle is not None:
            with contextlib.suppress(RPCError):
                await handle.terminate()  # already-completed executions cannot be terminated
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
