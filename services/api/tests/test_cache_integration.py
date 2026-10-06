"""Redis-gated integration tests for cache and rate-limit primitives."""

from __future__ import annotations

import os
import uuid

import pytest
import redis

from clipos_api.cache import CacheConfig, RedisCache
from clipos_api.rate_limit import RateLimitPolicy, RedisRateLimiter


@pytest.mark.skipif(not os.environ.get("CACHE_REDIS_URL"), reason="CACHE_REDIS_URL is not set")
def test_redis_cache_roundtrip_ttl_delete_and_incr() -> None:
    config = CacheConfig.from_env()
    client = redis.Redis.from_url(config.url)
    cache = RedisCache(config, client=client)
    key = f"test:{uuid.uuid4().hex}"
    cache.set(key, "payload", ttl_seconds=60)
    assert cache.get(key) == "payload"
    assert client.ttl(config.prefix + key) > 0
    cache.delete(key)
    assert cache.exists(key) is False
    assert cache.incr(key) == 1
    assert cache.incr(key) == 2
    client.delete(config.prefix + key)


@pytest.mark.skipif(not os.environ.get("CACHE_REDIS_URL"), reason="CACHE_REDIS_URL is not set")
def test_redis_rate_limiter_enforces_fixed_window() -> None:
    client = redis.Redis.from_url(os.environ["CACHE_REDIS_URL"])
    key = f"test:{uuid.uuid4().hex}"
    limiter = RedisRateLimiter(
        client,
        RateLimitPolicy(limit=2, window_seconds=2),
    )
    assert limiter.allow(key).allowed is True
    assert limiter.allow(key).allowed is True
    denied = limiter.allow(key)
    assert denied.allowed is False
    assert denied.retry_after_seconds > 0
    client.delete(f"clipos:rl:{key}:0")
