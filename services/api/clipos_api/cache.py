"""Redis-backed cache and fixed-window rate-limit primitives (M2, CLIP-012).

The cache contract is deliberately small so application code can depend on the
same namespaced key/value operations in tests and production. Redis is the
architecture-selected backend for cache and rate limiting; an in-memory
implementation supports deterministic tests and local use. Redis failures are
mapped to ``CacheUnavailable`` so callers can distinguish backend failure from
ordinary missing keys.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Protocol, runtime_checkable

import redis
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError

_CLOCK = Callable[[], float]
_REDACTED_URL = re.compile(r"//[^@/]+@")


class CacheConfigurationError(RuntimeError):
    """Raised when required CACHE_* environment variables are missing/invalid."""


class CacheUnavailable(RuntimeError):
    """A cache backend failed; redis errors do not escape this module."""


def _redact_url(url: str) -> str:
    """Remove URL credentials (typically an embedded password) for reprs."""
    return _REDACTED_URL.sub("//***@", url)


class CacheConfig(BaseModel):
    """Redis cache settings; credentials embedded in the URL are never repr'd."""

    model_config = ConfigDict(frozen=True)

    url: str
    prefix: str = "clipos:"
    default_ttl_seconds: int = Field(default=300, ge=1)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> CacheConfig:
        env = os.environ if environ is None else environ
        url = env.get("CACHE_REDIS_URL")
        if not url:
            raise CacheConfigurationError("missing required env var: CACHE_REDIS_URL")
        try:
            ttl = int(env.get("CACHE_DEFAULT_TTL_SECONDS", "300"))
        except ValueError as error:
            raise CacheConfigurationError("CACHE_DEFAULT_TTL_SECONDS must be an integer") from error
        return cls(
            url=url,
            prefix=env.get("CACHE_KEY_PREFIX", "clipos:"),
            default_ttl_seconds=ttl,
        )

    def __repr__(self) -> str:
        return (
            f"CacheConfig(url={_redact_url(self.url)!r}, prefix={self.prefix!r}, "
            f"default_ttl_seconds={self.default_ttl_seconds})"
        )

    def __str__(self) -> str:
        return self.__repr__()


@runtime_checkable
class KeyValueCache(Protocol):
    """Namespaced key/value cache contract."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...

    def delete(self, key: str) -> None: ...

    def exists(self, key: str) -> bool: ...

    def incr(self, key: str, amount: int = 1) -> int: ...


def _decode(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


class RedisCache:
    """``KeyValueCache`` backed by Redis with injectable redis client."""

    def __init__(self, config: CacheConfig, client: redis.Redis | None = None) -> None:
        self._config = config
        self._client = client if client is not None else self._build_client(config)

    @staticmethod
    def _build_client(config: CacheConfig) -> redis.Redis:
        return redis.Redis.from_url(config.url)

    def _key(self, key: str) -> str:
        return f"{self._config.prefix}{key}"

    def _wrap(self, error: RedisError) -> CacheUnavailable:
        return CacheUnavailable(f"cache backend unavailable: {error}")

    def get(self, key: str) -> str | None:
        try:
            return _decode(self._client.get(self._key(key)))
        except RedisError as error:
            raise self._wrap(error) from error

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        ttl = self._config.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        namespaced_key = self._key(key)
        try:
            self._client.set(namespaced_key, value)
            self._client.expire(namespaced_key, ttl)
        except RedisError as error:
            raise self._wrap(error) from error

    def delete(self, key: str) -> None:
        try:
            self._client.delete(self._key(key))
        except RedisError as error:
            raise self._wrap(error) from error

    def exists(self, key: str) -> bool:
        try:
            return bool(self._client.exists(self._key(key)))
        except RedisError as error:
            raise self._wrap(error) from error

    def incr(self, key: str, amount: int = 1) -> int:
        try:
            return int(self._client.incr(self._key(key), amount))
        except RedisError as error:
            raise self._wrap(error) from error


class InMemoryCache:
    """Thread-local-by-convention cache for tests and single-process fallbacks."""

    def __init__(self, clock: _CLOCK | None = None) -> None:
        self._clock = clock if clock is not None else lambda: 0.0
        self._values: dict[str, str] = {}
        self._expires_at: dict[str, float] = {}

    def _key(self, key: str) -> str:
        return f"clipos:{key}"

    def _live_value(self, key: str) -> str | None:
        value = self._values.get(key)
        if value is None:
            return None
        expires_at = self._expires_at.get(key)
        if expires_at is not None and self._clock() >= expires_at:
            self._values.pop(key, None)
            self._expires_at.pop(key, None)
            return None
        return value

    def get(self, key: str) -> str | None:
        return self._live_value(self._key(key))

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        namespaced_key = self._key(key)
        self._values[namespaced_key] = value
        ttl = 300 if ttl_seconds is None else ttl_seconds
        self._expires_at[namespaced_key] = self._clock() + ttl

    def delete(self, key: str) -> None:
        self._values.pop(self._key(key), None)
        self._expires_at.pop(self._key(key), None)

    def exists(self, key: str) -> bool:
        return self._live_value(self._key(key)) is not None

    def incr(self, key: str, amount: int = 1) -> int:
        namespaced_key = self._key(key)
        value = self._live_value(namespaced_key)
        current = int(value) if value is not None else 0
        updated = current + amount
        self._values[namespaced_key] = str(updated)
        self._expires_at.pop(namespaced_key, None)
        return updated
