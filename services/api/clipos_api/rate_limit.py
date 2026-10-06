"""Fixed-window rate-limit primitives backed by Redis or memory (M2, CLIP-012).

Each key is counted in a fixed epoch-based window. ``RedisRateLimiter`` uses
``INCR`` followed by ``EXPIRE`` only on the first increment. This avoids a
distributed transaction while keeping Redis as the single source of truth, but
a process crash between ``INCR`` and ``EXPIRE`` can leave an initial key
without a TTL. Production callers should keep this documented limitation in
mind; the in-memory twin uses the same fixed-window rule and injected clock so
unit tests can reproduce exact allow/deny sequences without sleeping.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

import redis
from pydantic import BaseModel, ConfigDict, Field

_CLOCK = Callable[[], float]
_DEFAULT_KEY_PREFIX = "clipos:"


class RateLimitPolicy(BaseModel):
    """Maximum requests allowed in each fixed window."""

    model_config = ConfigDict(frozen=True)

    limit: int = Field(ge=1)
    window_seconds: int = Field(ge=1)


class RateLimitResult(BaseModel):
    """Outcome of one ``allow`` call."""

    allowed: bool
    remaining: int
    retry_after_seconds: int
    reset_at: int

    model_config = {"frozen": True}


@runtime_checkable
class RateLimiter(Protocol):
    """Request admission contract for future API route wiring."""

    def allow(self, key: str) -> RateLimitResult: ...


class RedisRateLimiter:
    """Fixed-window limiter using Redis ``INCR``/``EXPIRE``."""

    def __init__(
        self,
        client: redis.Redis,
        policy: RateLimitPolicy,
        clock: _CLOCK | None = None,
    ) -> None:
        self._client = client
        self._policy = policy
        self._clock = clock if clock is not None else _default_clock
        self._key_prefix = os.environ.get("RATE_LIMIT_KEY_PREFIX", _DEFAULT_KEY_PREFIX)

    def _window_key(self, key: str, window_index: int) -> str:
        return f"{self._key_prefix}rl:{key}:{window_index}"

    def allow(self, key: str) -> RateLimitResult:
        now = self._clock()
        window = self._policy.window_seconds
        window_index = math.floor(now / window)
        window_key = self._window_key(key, window_index)
        count = self._client.incr(window_key)
        if count == 1:
            self._client.expire(window_key, window + 1)
        reset_at = (window_index + 1) * window
        remaining = max(0, self._policy.limit - count)
        allowed = count <= self._policy.limit
        retry_after = 0 if allowed else max(1, math.ceil(reset_at - now))
        return RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            retry_after_seconds=retry_after,
            reset_at=reset_at,
        )


class InMemoryRateLimiter:
    """Deterministic in-memory implementation of the Redis limiter contract."""

    def __init__(self, policy: RateLimitPolicy, clock: _CLOCK | None = None) -> None:
        self._policy = policy
        self._clock = clock if clock is not None else _default_clock
        self._key_prefix = os.environ.get("RATE_LIMIT_KEY_PREFIX", _DEFAULT_KEY_PREFIX)
        self._counts: dict[str, int] = {}
        self._window_ends: dict[str, int] = {}

    def _window_key(self, key: str, window_index: int) -> str:
        return f"{self._key_prefix}rl:{key}:{window_index}"

    def allow(self, key: str) -> RateLimitResult:
        now = self._clock()
        window = self._policy.window_seconds
        window_index = math.floor(now / window)
        window_key = self._window_key(key, window_index)
        reset_at = (window_index + 1) * window
        if self._window_ends.get(window_key) != reset_at:
            self._counts[window_key] = 0
            self._window_ends[window_key] = reset_at
        self._counts[window_key] += 1
        count = self._counts[window_key]
        remaining = max(0, self._policy.limit - count)
        allowed = count <= self._policy.limit
        retry_after = 0 if allowed else max(1, math.ceil(reset_at - now))
        return RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            retry_after_seconds=retry_after,
            reset_at=reset_at,
        )


def _default_clock() -> float:
    return time.time()
