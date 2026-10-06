"""In-process Temporal worker bootstrap (env-only configuration)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from clipos_api.temporal_config import TemporalConfig

_DEFAULT_TASK_QUEUE = "clipos-core"
_DEFAULT_MAX_CONCURRENT_ACTIVITIES = 100


@dataclass(frozen=True)
class WorkerConfig:
    """Worker runtime settings; credentials are never configured or stored."""

    temporal: TemporalConfig
    task_queue: str
    max_concurrent_activities: int

    @classmethod
    def from_env(
        cls,
        *,
        task_queue: str | None = None,
        max_concurrent_activities: str | None = None,
    ) -> WorkerConfig:
        """Build config from TEMPORAL_* variables (overridable for tests)."""
        raw_queue = (
            task_queue if task_queue is not None else os.environ.get("TEMPORAL_TASK_QUEUE", "")
        )
        raw_concurrency = (
            max_concurrent_activities
            if max_concurrent_activities is not None
            else os.environ.get("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "")
        )
        try:
            concurrency = (
                int(raw_concurrency)
                if raw_concurrency.strip()
                else _DEFAULT_MAX_CONCURRENT_ACTIVITIES
            )
        except ValueError as exc:
            raise ValueError(
                "TEMPORAL_MAX_CONCURRENT_ACTIVITIES must be a positive integer"
            ) from exc
        if concurrency <= 0:
            raise ValueError("TEMPORAL_MAX_CONCURRENT_ACTIVITIES must be a positive integer")
        return cls(
            temporal=TemporalConfig.from_env(),
            task_queue=raw_queue or _DEFAULT_TASK_QUEUE,
            max_concurrent_activities=concurrency,
        )


async def connect_client(config: WorkerConfig) -> Client:
    """Connect to the configured Temporal frontend (no credentials stored)."""
    return await Client.connect(config.temporal.address, namespace=config.temporal.namespace)


def build_worker(
    client: Client,
    config: WorkerConfig,
    workflows: Sequence[type],
    activities: Sequence[Callable[..., Any]],
) -> Worker:
    """Assemble a Worker bound to the configured task queue."""
    return Worker(
        client,
        task_queue=config.task_queue,
        workflows=list(workflows),
        activities=list(activities),
        max_concurrent_activities=config.max_concurrent_activities,
    )


async def run_worker_until_stopped(
    client: Client,
    config: WorkerConfig,
    workflows: Sequence[type],
    activities: Sequence[Callable[..., Any]],
) -> None:
    """Run the worker until interrupted or cancelled, then close gracefully.

    ``KeyboardInterrupt`` and ``asyncio.CancelledError`` both drain through the
    worker's close path; normal completion closes cleanly as well.
    """
    worker = build_worker(client, config, workflows, activities)
    try:
        # ``Worker.run()`` owns start/close; wrapping it in ``async with worker``
        # would start the bridge twice (Rust TaskLocals panic).
        await worker.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
