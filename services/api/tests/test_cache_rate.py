"""Unit tests for cache and fixed-window rate-limit primitives (CLIP-012)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from redis.exceptions import RedisError

from clipos_api.cache import (
    CacheConfig,
    CacheConfigurationError,
    CacheUnavailable,
    InMemoryCache,
    RedisCache,
)
from clipos_api.rate_limit import (
    InMemoryRateLimiter,
    RateLimitPolicy,
    RateLimitResult,
    RedisRateLimiter,
)


class FakeClock:
    """No-sleep deterministic clock in seconds since the epoch."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRedisClient:
    """Minimal redis client recording calls; no network."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.values: dict[str, bytes] = {}
        self.counters: dict[str, int] = {}
        self.fail_on = fail_on

    def _check(self, method: str) -> None:
        if method == self.fail_on:
            raise RedisError("backend unavailable")

    def get(self, name: str) -> bytes | None:
        self.calls.append(("get", {"name": name}))
        self._check("get")
        return self.values.get(name)

    def set(self, name: str, value: str) -> bool:
        self.calls.append(("set", {"name": name, "value": value}))
        self._check("set")
        self.values[name] = value.encode("utf-8")
        return True

    def expire(self, name: str, seconds: int) -> bool:
        self.calls.append(("expire", {"name": name, "seconds": seconds}))
        self._check("expire")
        return True

    def delete(self, name: str) -> int:
        self.calls.append(("delete", {"name": name}))
        self._check("delete")
        existed = name in self.values
        self.values.pop(name, None)
        return int(existed)

    def exists(self, name: str) -> int:
        self.calls.append(("exists", {"name": name}))
        self._check("exists")
        return int(name in self.values)

    def incr(self, name: str, amount: int = 1) -> int:
        self.calls.append(("incr", {"name": name, "amount": amount}))
        self._check("incr")
        self.counters[name] = self.counters.get(name, 0) + amount
        return self.counters[name]


@pytest.fixture()
def fake_client() -> FakeRedisClient:
    return FakeRedisClient()


@pytest.fixture()
def cache_config() -> CacheConfig:
    return CacheConfig(url="redis://user:supersecret@localhost:6380/0", default_ttl_seconds=45)


def test_cache_config_reads_env_and_defaults() -> None:
    config = CacheConfig.from_env({"CACHE_REDIS_URL": "redis://localhost:6380/0"})
    assert config.prefix == "clipos:"
    assert config.default_ttl_seconds == 300
    configured = CacheConfig.from_env(
        {
            "CACHE_REDIS_URL": "redis://localhost:6380/0",
            "CACHE_KEY_PREFIX": "clipos-test:",
            "CACHE_DEFAULT_TTL_SECONDS": "17",
        }
    )
    assert configured.prefix == "clipos-test:"
    assert configured.default_ttl_seconds == 17
    with pytest.raises(CacheConfigurationError):
        CacheConfig.from_env({})
    with pytest.raises(CacheConfigurationError):
        CacheConfig.from_env(
            {"CACHE_REDIS_URL": "redis://localhost", "CACHE_DEFAULT_TTL_SECONDS": "x"}
        )


def test_cache_config_redacts_embedded_password(cache_config: CacheConfig) -> None:
    rendered = repr(cache_config)
    assert "supersecret" not in rendered
    assert "user:***@localhost" not in rendered
    assert "***@localhost" in rendered
    assert str(cache_config) == rendered


def test_redis_cache_namespaces_every_operation(
    cache_config: CacheConfig, fake_client: FakeRedisClient
) -> None:
    cache = RedisCache(cache_config, client=fake_client)
    assert cache.get("missing") is None
    cache.set("media", "value", ttl_seconds=120)
    assert cache.get("media") == "value"
    assert cache.exists("media") is True
    assert cache.incr("counter") == 1
    cache.delete("media")
    methods = [method for method, _ in fake_client.calls]
    assert methods == ["get", "set", "expire", "get", "exists", "incr", "delete"]
    assert all(params["name"].startswith("clipos:") for _, params in fake_client.calls)
    assert fake_client.calls[-1][1]["name"] == "clipos:media"


def test_redis_cache_expires_with_correct_seconds(
    cache_config: CacheConfig, fake_client: FakeRedisClient
) -> None:
    cache = RedisCache(cache_config, client=fake_client)
    cache.set("ttl", "value")
    cache.set("override", "value", ttl_seconds=17)
    expires = [params for method, params in fake_client.calls if method == "expire"]
    assert expires[0]["seconds"] == 45
    assert expires[1]["seconds"] == 17


def test_redis_cache_wraps_redis_errors(fake_client: FakeRedisClient) -> None:
    cache = RedisCache(
        CacheConfig(url="redis://localhost:6380/0"), client=FakeRedisClient(fail_on="get")
    )
    with pytest.raises(CacheUnavailable, match="cache backend unavailable"):
        cache.get("key")


def test_in_memory_cache_roundtrip_and_expiry() -> None:
    clock = FakeClock()
    cache = InMemoryCache(clock=clock)
    cache.set("item", "first", ttl_seconds=10)
    assert cache.get("item") == "first"
    clock.advance(9.999)
    assert cache.exists("item") is True
    clock.advance(0.001)
    assert cache.get("item") is None
    assert cache.exists("item") is False
    cache.set("item", "second")
    assert cache.get("item") == "second"
    cache.delete("item")
    assert cache.exists("item") is False


def test_in_memory_cache_namespaces_and_increments() -> None:
    cache = InMemoryCache(FakeClock())
    assert cache.incr("requests") == 1
    assert cache.incr("requests") == 2
    assert cache.get("requests") == "2"


def rate_limiter_results(limiter: Any, key: str) -> list[tuple[bool, int, int, int]]:
    return [
        (result.allowed, result.remaining, result.retry_after_seconds, result.reset_at)
        for result in (limiter.allow(key) for _ in range(5))
    ]


@pytest.mark.parametrize(("limit", "window"), [(3, 10), (1, 5), (2, 7)])
def test_memory_and_redis_limiters_match_contract(
    fake_client: FakeRedisClient, limit: int, window: int
) -> None:
    policy = RateLimitPolicy(limit=limit, window_seconds=window)
    clock = FakeClock(90.0)
    memory = InMemoryRateLimiter(policy, clock=clock)
    redis_limiter = RedisRateLimiter(fake_client, policy, clock=clock)
    assert rate_limiter_results(memory, "contract") == rate_limiter_results(
        redis_limiter, "contract"
    )


def test_redis_rate_limiter_allows_limit_then_denies(fake_client: FakeRedisClient) -> None:
    clock = FakeClock(100.0)
    policy = RateLimitPolicy(limit=3, window_seconds=10)
    limiter = RedisRateLimiter(fake_client, policy, clock=clock)
    allowed = [limiter.allow("route") for _ in range(3)]
    assert [result.allowed for result in allowed] == [True, True, True]
    assert [result.remaining for result in allowed] == [2, 1, 0]
    denied = limiter.allow("route")
    assert denied.allowed is False
    assert denied.remaining == 0
    assert denied.retry_after_seconds == 10
    assert denied.reset_at == 110
    incr_calls = [params for method, params in fake_client.calls if method == "incr"]
    assert incr_calls == [
        {"name": "clipos:rl:route:10", "amount": 1},
        {"name": "clipos:rl:route:10", "amount": 1},
        {"name": "clipos:rl:route:10", "amount": 1},
        {"name": "clipos:rl:route:10", "amount": 1},
    ]
    expire_calls = [params for method, params in fake_client.calls if method == "expire"]
    assert expire_calls == [{"name": "clipos:rl:route:10", "seconds": 11}]


def test_memory_rate_limiter_resets_on_window_rollover() -> None:
    clock = FakeClock(0.0)
    limiter = InMemoryRateLimiter(RateLimitPolicy(limit=1, window_seconds=5), clock=clock)
    assert limiter.allow("key").allowed is True
    denied = limiter.allow("key")
    assert denied.allowed is False
    assert denied.retry_after_seconds > 0
    clock.advance(5.0)
    reset = limiter.allow("key")
    assert reset.allowed is True
    assert reset.remaining == 0


def test_rate_limit_policy_rejects_nonpositive_values() -> None:
    with pytest.raises(ValidationError):
        RateLimitPolicy(limit=0, window_seconds=10)
    with pytest.raises(ValidationError):
        RateLimitPolicy(limit=1, window_seconds=0)


def test_rate_limit_result_is_frozen() -> None:
    result = RateLimitResult(allowed=True, remaining=1, retry_after_seconds=0, reset_at=10)
    with pytest.raises(ValidationError):
        result.allowed = False
