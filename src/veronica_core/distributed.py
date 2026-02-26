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
        # Track the Redis total at the moment of failover seeding.  This is the
        # "base" already known to Redis; _reconcile_on_reconnect must flush only
        # the delta above this base to avoid double-counting.
        self._fallback_seed_base: float = 0.0
        self._connect()

    def _connect(self) -> None:
        try:
            import redis

            self._client = redis.from_url(self._redis_url, decode_responses=True)
            self._client.ping()
            # Successful connection: clear fallback flag so Redis is used again.
            self._using_fallback = False
        except Exception as exc:
            if self._fallback_on_error:
                logger.warning(
                    "RedisBudgetBackend: cannot connect to Redis (%s). Falling back to LocalBudgetBackend.",
                    exc,
                )
                self._using_fallback = True
            else:
                raise

    # Minimum seconds to wait between reconnect attempts.  Prevents hot-loop
    # log storms and latency spikes when Redis is down for an extended period.
    _RECONNECT_INTERVAL: float = 5.0

    def _reconcile_on_reconnect(self) -> bool:
        """Reconcile locally accumulated spend into Redis after reconnection.

        During fallback, costs accumulate in the local backend.  When Redis
        becomes reachable again we flush the **delta** — the amount accumulated
        *since* failover — with INCRBYFLOAT.  The seed base (Redis total at the
        moment of failover) is already in Redis and must NOT be counted again.

        Formula:
            delta = fallback.get() - _fallback_seed_base

        After successful reconciliation the local backend and seed base are reset.

        Returns:
            True if reconciliation succeeded (or there was nothing to reconcile),
            False if the Redis write failed (delta is preserved in local fallback).
        """
        fallback_total = self._fallback.get()
        delta = fallback_total - getattr(self, "_fallback_seed_base", 0.0)
        if delta <= 0.0:
            # Nothing new to flush; reset local state.
            self._fallback.reset()
            self._fallback_seed_base = 0.0
            return True
        try:
            pipe = self._client.pipeline()
            pipe.incrbyfloat(self._key, delta)
            pipe.expire(self._key, self._ttl)
            pipe.execute()
            logger.info(
                "RedisBudgetBackend: reconciled %.6f USD of fallback spend into Redis.",
                delta,
            )
        except Exception as exc:
            logger.error(
                "RedisBudgetBackend: reconciliation failed (%s) — fallback delta preserved.",
                exc,
            )
            return False
        self._fallback.reset()
        self._fallback_seed_base = 0.0
        return True

    def _try_reconnect(self) -> bool:
        """Attempt to reconnect to Redis. Returns True if connection succeeded.

        ``_using_fallback`` is only cleared after a successful reconciliation so
        that a reconcile failure leaves the backend on fallback with its delta
        intact — preventing undercount of accumulated spend.

        Reconnect attempts are rate-limited via ``_last_reconnect_attempt`` to
        avoid hot-loop log storms during extended Redis outages.
        """
        import time as _time

        now = _time.monotonic()
        last = getattr(self, "_last_reconnect_attempt", 0.0)
        if now - last < self._RECONNECT_INTERVAL:
            return False
        self._last_reconnect_attempt = now

        try:
            self._connect()
        except Exception:
            return False

        # _connect() sets _using_fallback=False on success, True on failure.
        if self._using_fallback:
            return False

        # _connect() cleared _using_fallback; we're connected but not yet safe
        # to route new adds to Redis until the local delta is flushed.
        # Restore fallback mode while we attempt reconciliation so that a
        # failure leaves the backend in a consistent state.
        self._using_fallback = True
        reconciled = self._reconcile_on_reconnect()
        if reconciled:
            self._using_fallback = False
            logger.info("RedisBudgetBackend: reconnected to Redis successfully.")
            return True
        # Reconcile failed — stay on fallback.
        return False

    def _seed_fallback_from_redis(self) -> None:
        """Seed the local fallback with the current Redis total before failover.

        When the backend fails over mid-session, ``_fallback`` starts at 0.0.
        Without seeding, subsequent ``add()`` calls return local-only totals
        that exclude prior Redis spend, allowing overspend.  We read the current
        Redis value once and pre-load the fallback so that totals remain
        monotonically correct across the failover boundary.

        The seeded value is stored in ``_fallback_seed_base`` so that
        ``_reconcile_on_reconnect`` can flush only the **delta**
        (local increments since failover) and avoid double-counting the base
        total already present in Redis.

        Called while the Redis connection is still live (just before transition).
        Safe to skip on error — worst case is slightly permissive enforcement,
        not a crash.
        """
        try:
            val = self._client.get(self._key)
            redis_total = float(val) if val is not None else 0.0
            if redis_total > 0.0:
                self._fallback.reset()
                self._fallback.add(redis_total)
                self._fallback_seed_base = redis_total
                logger.info(
                    "RedisBudgetBackend: seeded local fallback with %.6f USD from Redis.",
                    redis_total,
                )
            else:
                self._fallback_seed_base = 0.0
        except Exception as exc:
            self._fallback_seed_base = 0.0
            logger.warning(
                "RedisBudgetBackend: could not seed fallback from Redis (%s) — "
                "budget enforcement may be permissive during outage.",
                exc,
            )

    def add(self, amount: float) -> float:
        # If previously using fallback, attempt to reconnect (rate-limited).
        if self._using_fallback and self._fallback_on_error:
            with self._lock:
                if self._using_fallback:
                    self._try_reconnect()

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
                # Seed fallback with last known Redis total and switch atomically.
                # The lock + double-check prevents concurrent threads from seeding
                # multiple times or losing increments during the transition.
                with self._lock:
                    if not self._using_fallback:
                        # Only the first thread to acquire the lock performs the
                        # seed; subsequent threads see _using_fallback=True and
                        # skip straight to fallback.add() below.
                        self._seed_fallback_from_redis()
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
