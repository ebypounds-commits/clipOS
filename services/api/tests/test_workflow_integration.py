import asyncio
import contextlib
import os
import uuid
from datetime import timedelta

import pytest

from clipos_api.reference_workflow import (
    FrameworkSelfTestInput,
    FrameworkSelfTestWorkflow,
    evaluate_approval,
    record_idempotent_step,
    use_idempotency_store,
)
from clipos_api.worker import WorkerConfig, connect_client, run_worker_until_stopped
from clipos_api.workflow_framework import business_key


def _input(key: str) -> FrameworkSelfTestInput:
    return FrameworkSelfTestInput(
        metadata={
            "organization_id": "org-1",
            "brand_id": "brand-1",
            "content_id": "content-1",
            "policy_version": "v1",
            "correlation_id": "corr-int-1",
        },
        business_key=key,
        wait_seconds=0.05,
        approve_after_seconds=0.05,
        approval_ttl_seconds=60.0,
    )


@pytest.mark.skipif(not os.environ.get("TEMPORAL_ADDRESS"), reason="live Temporal not configured")
def test_reference_workflow_live_idempotency() -> None:
    asyncio.run(_run_live_workflow())


async def _run_live_workflow() -> None:
    config = WorkerConfig.from_env()
    client = await connect_client(config)
    store: dict[str, int] = {}
    use_idempotency_store(store)
    activities = [record_idempotent_step, evaluate_approval]
    key = business_key("render", project_id="proj-int", render_version="1")
    worker_task = asyncio.create_task(
        run_worker_until_stopped(client, config, [FrameworkSelfTestWorkflow], activities)
    )
    try:
        first = await asyncio.wait_for(
            client.execute_workflow(
                FrameworkSelfTestWorkflow.run,
                _input(key),
                id=f"framework-self-test-{uuid.uuid4()}",
                task_queue=config.task_queue,
                execution_timeout=timedelta(seconds=30),
            ),
            timeout=60,
        )
        assert first["outcome"] == "approved"
        assert first["correlation_id"] == "corr-int-1"
        assert first["first_run"] is True
        second = await asyncio.wait_for(
            client.execute_workflow(
                FrameworkSelfTestWorkflow.run,
                _input(key),
                id=f"framework-self-test-{uuid.uuid4()}",
                task_queue=config.task_queue,
                execution_timeout=timedelta(seconds=30),
            ),
            timeout=60,
        )
        assert second["outcome"] == "approved"
        assert second["first_run"] is False
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
