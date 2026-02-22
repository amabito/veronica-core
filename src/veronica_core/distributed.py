"""Distributed budget backends for cross-process cost coordination."""
from __future__ import annotations

import logging
import threading
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class BudgetBackend(Protocol):
    def add(self, amount: float) -> float: ...
    def get(self) -> float: ...
    def reset(self) -> None: ...
    def close(self) -> None: ...


class LocalBudgetBackend:
    """In-process budget backend. Thread-safe. Default behavior."""

    def __init__(self) -> None:
        self._cost: float = 0.0
        self._lock = threading.Lock()

    def add(self, amount: float) -> float:
        with self._lock:
            self._cost += amount
            return self._cost

    def get(self) -> float:
        with self._lock:
            return self._cost

    def reset(self) -> None:
        with self._lock:
            self._cost = 0.0

    def close(self) -> None:
        pass


class RedisBudgetBackend:
    """Redis-backed budget backend for cross-process cost coordination.

    Uses INCRBYFLOAT for atomic float increments.
    Falls back to LocalBudgetBackend if Redis is unreachable.
    """

    KEY_PREFIX = "veronica:budget:"

    def __init__(
        self,
        redis_url: str,
        chain_id: str,
        ttl_seconds: int = 3600,
        fallback_on_error: bool = True,
    ) -> None:
        self._redis_url = redis_url
        self._chain_id = chain_id
        self._key = f"{self.KEY_PREFIX}{chain_id}"
        self._ttl = ttl_seconds
        self._fallback_on_error = fallback_on_error
        self._fallback = LocalBudgetBackend()
        self._using_fallback = False
        self._client = None
        self._lock = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        try:
            import redis

            self._client = redis.from_url(self._redis_url, decode_responses=True)
            self._client.ping()
        except Exception as exc:
            if self._fallback_on_error:
                logger.warning(
                    "RedisBudgetBackend: cannot connect to Redis (%s). Falling back to LocalBudgetBackend.",
                    exc,
                )
                self._using_fallback = True
            else:
                raise

    def add(self, amount: float) -> float:
        if self._using_fallback or self._client is None:
            return self._fallback.add(amount)
        try:
            pipe = self._client.pipeline()
            pipe.incrbyfloat(self._key, amount)
            pipe.expire(self._key, self._ttl)
            results = pipe.execute()
            return float(results[0])
        except Exception as exc:
            if self._fallback_on_error:
                logger.error(
                    "RedisBudgetBackend.add failed: %s — using local fallback", exc
                )
                self._using_fallback = True
                return self._fallback.add(amount)
            raise

    def get(self) -> float:
        if self._using_fallback or self._client is None:
            return self._fallback.get()
        try:
            val = self._client.get(self._key)
            return float(val) if val is not None else 0.0
        except Exception as exc:
            if self._fallback_on_error:
                logger.error("RedisBudgetBackend.get failed: %s", exc)
                return self._fallback.get()
            raise

    def reset(self) -> None:
        if self._using_fallback or self._client is None:
            self._fallback.reset()
            return
        try:
            self._client.delete(self._key)
        except Exception as exc:
            if self._fallback_on_error:
                logger.error("RedisBudgetBackend.reset failed: %s", exc)
            else:
                raise

    def close(self) -> None:
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass

    @property
    def is_using_fallback(self) -> bool:
        return self._using_fallback


def get_default_backend(
    redis_url: str | None = None,
    chain_id: str = "default",
    ttl_seconds: int = 3600,
) -> BudgetBackend:
    """Factory: returns RedisBudgetBackend if redis_url given, else LocalBudgetBackend."""
    if redis_url:
        return RedisBudgetBackend(
            redis_url=redis_url, chain_id=chain_id, ttl_seconds=ttl_seconds
        )
    return LocalBudgetBackend()
