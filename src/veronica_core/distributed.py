"""Distributed budget backends and circuit breakers for cross-process coordination."""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# H1: INCRBYFLOAT accumulates IEEE-754 rounding errors across many small increments.
# Use this epsilon when comparing Redis-backed float totals to hard limits to avoid
# spurious under-enforcement (e.g. 9.999999999999998 failing a 10.0 limit check).
_BUDGET_EPSILON: float = 1e-9

# Reservation timeout: auto-rollback after this many seconds to prevent leaks
# when the caller crashes between reserve() and commit()/rollback().
_RESERVATION_TIMEOUT_S: float = 60.0


class ReservationExpiredError(Exception):
    """Raised when a reservation ID has passed its deadline."""


def _redact_exc(exc: BaseException) -> str:
    """Return exception type and message with Redis URLs redacted.

    Prevents credential leakage when ``redis://user:password@host/...``
    appears in exception strings (e.g. ``ConnectionError``).

    Handles ``redis://``, ``rediss://``, ``redis+ssl://``, ``rediss+ssl://``
    (case-insensitive), and passwords containing literal ``@`` characters.
    """
    msg = str(exc)
    # Redact user:password in Redis URLs.
    # - ``rediss?`` matches redis:// and rediss://
    # - ``(?:\\+ssl)?`` matches optional +ssl suffix
    # - ``\\S+@`` greedy match handles passwords with literal '@' (backtracks to last @)
    # - ``(?=\\S)`` ensures the @ is followed by a hostname, not trailing whitespace
    msg = re.sub(
        r"(rediss?(?:\+ssl)?://)\S+@(?=\S)",
        r"\1***@",
        msg,
        flags=re.IGNORECASE,
    )
    return f"{type(exc).__name__}: {msg}"


@runtime_checkable
class BudgetBackend(Protocol):
    def add(self, amount: float) -> float: ...
    def get(self) -> float: ...
    def reset(self) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class ReservableBudgetBackend(BudgetBackend, Protocol):
    """Extended protocol for backends that support two-phase reserve/commit/rollback."""

    def reserve(self, amount: float, ceiling: float) -> str: ...
    def commit(self, reservation_id: str) -> float: ...
    def rollback(self, reservation_id: str) -> None: ...
    def get_reserved(self) -> float: ...


class LocalBudgetBackend:
    """In-process budget backend. Thread-safe. Default behavior.

    Supports two-phase reserve/commit/rollback for atomic budget accounting.
    Reservations expire after _RESERVATION_TIMEOUT_S seconds to prevent leaks.
    """

    def __init__(self) -> None:
        self._cost: float = 0.0
        # _reservations: rid -> (amount, deadline_monotonic)
        self._reservations: dict[str, tuple[float, float]] = {}
        self._reserved_total: float = 0.0
        self._lock = threading.Lock()

    def add(self, amount: float) -> float:
        with self._lock:
            self._cost += amount
            return self._cost

    def get(self) -> float:
        with self._lock:
            return self._cost

    def get_reserved(self) -> float:
        """Return the total amount currently held in active reservations."""
        with self._lock:
            total = self._total_reserved_locked()
            # Clamp to zero to avoid negative epsilon from float arithmetic.
            return max(0.0, total)

    def reserve(self, amount: float, ceiling: float) -> str:
        """Atomically reserve *amount* against *ceiling*.

        Checks committed + pending_reserved + amount <= ceiling + epsilon.
        Returns a reservation ID string.
        Raises OverflowError if the ceiling would be exceeded.
        Raises ValueError for invalid amount (NaN, Inf, negative, zero).
        """
        if not (amount > 0 and amount < float("inf")):
            raise ValueError(
                f"reserve() amount must be positive and finite, got {amount!r}"
            )
        with self._lock:
            reserved_total = self._total_reserved_locked()
            if self._cost + reserved_total + amount > ceiling + _BUDGET_EPSILON:
                raise OverflowError(
                    f"Budget ceiling {ceiling:.6f} would be exceeded: "
                    f"committed={self._cost:.6f}, reserved={reserved_total:.6f}, "
                    f"requested={amount:.6f}"
                )
            rid = str(uuid.uuid4())
            deadline = time.monotonic() + _RESERVATION_TIMEOUT_S
            self._reservations[rid] = (amount, deadline)
            self._reserved_total += amount
            return rid

    def commit(self, reservation_id: str) -> float:
        """Commit a reservation: move it from pending to committed cost.

        Returns the new total committed cost.
        Raises KeyError if the reservation_id is not found (already expired/rolled back).
        """
        with self._lock:
            self._expire_reservations_locked()
            if reservation_id not in self._reservations:
                raise KeyError(
                    f"Reservation {reservation_id!r} not found (expired or already committed/rolled back)"
                )
            amount, _ = self._reservations.pop(reservation_id)
            self._reserved_total -= amount
            self._cost += amount
            return self._cost

    def rollback(self, reservation_id: str) -> None:
        """Roll back a reservation without charging cost.

        Raises KeyError if the reservation_id is not found.
        """
        with self._lock:
            self._expire_reservations_locked()
            if reservation_id not in self._reservations:
                raise KeyError(
                    f"Reservation {reservation_id!r} not found (expired or already committed/rolled back)"
                )
            amount, _ = self._reservations.pop(reservation_id)
            self._reserved_total -= amount

    def reset(self) -> None:
        with self._lock:
            self._cost = 0.0
            self._reservations.clear()
            self._reserved_total = 0.0

    def close(self) -> None:
        pass

    def _expire_reservations_locked(self) -> None:
        """Remove expired reservations. Must be called with self._lock held."""
        now = time.monotonic()
        expired = [
            rid for rid, (_, deadline) in self._reservations.items() if now > deadline
        ]
        for rid in expired:
            amt, _ = self._reservations.pop(rid)
            self._reserved_total -= amt

    def _total_reserved_locked(self) -> float:
        """Return total active reservation amount. Call with self._lock held."""
        self._expire_reservations_locked()
        # Reset accumulator when empty to prevent float drift.
        if not self._reservations:
            self._reserved_total = 0.0
        return self._reserved_total


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
                    _redact_exc(exc),
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

        M6 INVARIANT: This method is always called from _try_reconnect(), which is
        always called from add() while self._lock is held.  Concurrent add() calls
        therefore cannot slip in between fallback.get() and fallback.reset(), so
        the delta computation is race-free.  The LocalBudgetBackend operations
        below each hold their own internal lock (re-entrant safe for a different
        lock object), so there is no deadlock risk.

        Returns:
            True if reconciliation succeeded (or there was nothing to reconcile),
            False if the Redis write failed (delta is preserved in local fallback).
        """
        fallback_total = self._fallback.get()
        delta = fallback_total - self._fallback_seed_base
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
                _redact_exc(exc),
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

        **Must be called with ``self._lock`` already held by the caller.**
        ``add()`` acquires the lock for the entire check-and-dispatch block
        (H4 TOCTOU fix), so ``_last_reconnect_attempt`` reads/writes here are
        automatically serialised — no additional lock acquisition needed.
        """
        now = time.monotonic()
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
                _redact_exc(exc),
            )

    def add(self, amount: float) -> float:
        # Hold the lock for the entire check-and-dispatch to prevent TOCTOU:
        # without the lock, a concurrent thread can flip _using_fallback between
        # the outer read (reconnect guard) and the dispatch read below, causing
        # either double-routing or missed fallback transitions.
        with self._lock:
            if self._using_fallback and self._fallback_on_error:
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
                    "RedisBudgetBackend.add failed: %s — using local fallback",
                    _redact_exc(exc),
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
        # Capture client reference under lock to prevent TOCTOU: without capturing,
        # _using_fallback could flip to True between the check (L295) and the Redis
        # read (L298), causing the Redis read to execute against a stale/closed client.
        with self._lock:
            if self._using_fallback or self._client is None:
                return self._fallback.get()
            client = self._client
        try:
            val = client.get(self._key)
            return float(val) if val is not None else 0.0
        except Exception as exc:
            if self._fallback_on_error:
                logger.error("RedisBudgetBackend.get failed: %s", _redact_exc(exc))
                return self._fallback.get()
            raise

    def reset(self) -> None:
        # Hold lock for check-and-dispatch to prevent TOCTOU (same rationale as add()).
        with self._lock:
            if self._using_fallback or self._client is None:
                self._fallback.reset()
                return
        try:
            reservations_key = f"{self._key}:reservations"
            pipe = self._client.pipeline()
            pipe.delete(self._key)
            pipe.delete(reservations_key)
            pipe.execute()
        except Exception as exc:
            if self._fallback_on_error:
                logger.error("RedisBudgetBackend.reset failed: %s", _redact_exc(exc))
            else:
                raise

    def get_reserved(self) -> float:
        """Return total amount held in active reservations.

        Falls back to local backend if Redis unavailable.
        """
        with self._lock:
            if self._using_fallback or self._client is None:
                return self._fallback.get_reserved()
            client = self._client
        try:
            reservations_key = f"{self._key}:reservations"
            vals = client.hvals(reservations_key)
            now = time.time()
            total = 0.0
            for v in vals:
                sep = v.find(":")
                if sep >= 0:
                    amt = float(v[:sep])
                    dl = float(v[sep + 1 :])
                    if dl > now:
                        total += amt
            return total
        except Exception as exc:
            if self._fallback_on_error:
                logger.error(
                    "RedisBudgetBackend.get_reserved failed: %s", _redact_exc(exc)
                )
                return self._fallback.get_reserved()
            raise

    def reserve(self, amount: float, ceiling: float) -> str:
        """Atomically reserve *amount* against *ceiling* in Redis.

        Returns a reservation ID. Raises OverflowError if ceiling exceeded.
        Falls back to local backend if Redis unavailable.
        Raises ValueError for invalid amount (NaN, Inf, negative, zero).
        """
        if not (amount > 0 and amount < float("inf")):
            raise ValueError(
                f"reserve() amount must be positive and finite, got {amount!r}"
            )
        with self._lock:
            if self._using_fallback or self._client is None:
                return self._fallback.reserve(amount, ceiling)
            client = self._client

        try:
            reservations_key = f"{self._key}:reservations"
            rid = str(uuid.uuid4())
            now = time.time()
            deadline = now + _RESERVATION_TIMEOUT_S

            # Lua script for atomic reserve with ceiling check
            lua_reserve = """
local committed_key = KEYS[1]
local reservations_key = KEYS[2]
local amount = tonumber(ARGV[1])
local ceiling = tonumber(ARGV[2])
local rid = ARGV[3]
local deadline = tonumber(ARGV[4])
local now = tonumber(ARGV[5])

-- Single-pass: sweep expired + sum active via HGETALL
local all_pairs = redis.call('HGETALL', reservations_key)
local reserved_total = 0.0
for i = 1, #all_pairs, 2 do
    local r = all_pairs[i]
    local v = all_pairs[i + 1]
    local sep = string.find(v, ':')
    if sep then
        local dl = tonumber(string.sub(v, sep + 1)) or 0.0
        if dl <= now then
            redis.call('HDEL', reservations_key, r)
        else
            local amt = tonumber(string.sub(v, 1, sep - 1)) or 0.0
            reserved_total = reserved_total + amt
        end
    end
end

local committed_str = redis.call('GET', committed_key)
local committed = tonumber(committed_str) or 0.0

if committed + reserved_total + amount > ceiling + 1e-9 then
    return redis.error_reply('ERR ceiling exceeded')
end

redis.call('HSET', reservations_key, rid, amount .. ':' .. deadline)
return 1
"""
            result = client.eval(
                lua_reserve,
                2,
                self._key,
                reservations_key,
                str(amount),
                str(ceiling),
                rid,
                str(deadline),
                str(now),
            )
            if result != 1:
                raise OverflowError(f"Budget ceiling {ceiling:.6f} would be exceeded")
            return rid
        except Exception as exc:
            exc_str = str(exc)
            if "ceiling exceeded" in exc_str:
                raise OverflowError(
                    f"Budget ceiling {ceiling:.6f} would be exceeded"
                ) from exc
            if self._fallback_on_error:
                logger.error(
                    "RedisBudgetBackend.reserve failed: %s — using local fallback",
                    _redact_exc(exc),
                )
                with self._lock:
                    if not self._using_fallback:
                        self._seed_fallback_from_redis()
                        self._using_fallback = True
                return self._fallback.reserve(amount, ceiling)
            raise

    def commit(self, reservation_id: str) -> float:
        """Commit a reservation in Redis: move amount to committed cost.

        Returns the new total committed cost.
        Raises KeyError if the reservation_id is not found.
        """
        with self._lock:
            if self._using_fallback or self._client is None:
                return self._fallback.commit(reservation_id)
            client = self._client

        try:
            reservations_key = f"{self._key}:reservations"
            lua_commit = """
local committed_key = KEYS[1]
local reservations_key = KEYS[2]
local rid = ARGV[1]
local ttl = tonumber(ARGV[2])

local v = redis.call('HGET', reservations_key, rid)
if v == nil or v == false then
    return redis.error_reply('ERR reservation not found: ' .. rid)
end

local sep = string.find(v, ':')
local amount = 0.0
if sep then
    amount = tonumber(string.sub(v, 1, sep - 1)) or 0.0
else
    amount = tonumber(v) or 0.0
end

redis.call('HDEL', reservations_key, rid)
local new_total = redis.call('INCRBYFLOAT', committed_key, amount)
if ttl ~= nil and ttl > 0 then
    redis.call('EXPIRE', committed_key, ttl)
end
return tostring(new_total)
"""
            result = client.eval(
                lua_commit,
                2,
                self._key,
                reservations_key,
                reservation_id,
                str(self._ttl),
            )
            return float(result)
        except Exception as exc:
            exc_str = str(exc)
            if "reservation not found" in exc_str:
                raise KeyError(f"Reservation {reservation_id!r} not found") from exc
            if self._fallback_on_error:
                logger.error(
                    "RedisBudgetBackend.commit failed: %s — using local fallback",
                    _redact_exc(exc),
                )
                with self._lock:
                    if not self._using_fallback:
                        self._seed_fallback_from_redis()
                        self._using_fallback = True
                return self._fallback.commit(reservation_id)
            raise

    def rollback(self, reservation_id: str) -> None:
        """Roll back a reservation in Redis without charging cost.

        Raises KeyError if the reservation_id is not found.
        """
        with self._lock:
            if self._using_fallback or self._client is None:
                self._fallback.rollback(reservation_id)
                return
            client = self._client

        try:
            reservations_key = f"{self._key}:reservations"
            lua_rollback = """
local reservations_key = KEYS[1]
local rid = ARGV[1]

local existed = redis.call('HDEL', reservations_key, rid)
if existed == 0 then
    return redis.error_reply('ERR reservation not found: ' .. rid)
end
return 1
"""
            client.eval(lua_rollback, 1, reservations_key, reservation_id)
        except Exception as exc:
            exc_str = str(exc)
            if "reservation not found" in exc_str:
                raise KeyError(f"Reservation {reservation_id!r} not found") from exc
            if self._fallback_on_error:
                logger.error(
                    "RedisBudgetBackend.rollback failed: %s — using local fallback",
                    _redact_exc(exc),
                )
                with self._lock:
                    if not self._using_fallback:
                        self._seed_fallback_from_redis()
                        self._using_fallback = True
                try:
                    self._fallback.rollback(reservation_id)
                except KeyError:
                    pass
            else:
                raise

    def close(self) -> None:
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            # Intentionally swallowed: close() is best-effort cleanup; callers
            # must not observe errors from a backend that is already shut down.
            pass

    @property
    def is_using_fallback(self) -> bool:
        with self._lock:
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


# ---------------------------------------------------------------------------
# Backward-compatible re-exports from distributed_circuit_breaker.py
# ---------------------------------------------------------------------------

from veronica_core.distributed_circuit_breaker import (  # noqa: E402, F401
    CircuitSnapshot,
    DistributedCircuitBreaker,
    _LUA_CHECK,
    _LUA_RECORD_FAILURE,
    _LUA_RECORD_SUCCESS,
    get_default_circuit_breaker,
)
