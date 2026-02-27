"""Distributed budget backends and circuit breakers for cross-process coordination."""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Dict, Optional, Protocol, runtime_checkable

from veronica_core.circuit_breaker import CircuitBreaker, CircuitState, FailurePredicate
from veronica_core.runtime_policy import PolicyContext, PolicyDecision

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


# ---------------------------------------------------------------------------
# Lua scripts for atomic Redis operations
# ---------------------------------------------------------------------------

# record_failure Lua script:
# KEYS[1] = hash key
# ARGV[1] = failure_threshold (int)
# ARGV[2] = current_time (float, Unix timestamp)
# ARGV[3] = ttl_seconds (int)
# Returns: new failure_count (int)
_LUA_RECORD_FAILURE = """
local key = KEYS[1]
local threshold = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

-- Initialize fields if missing
if redis.call('EXISTS', key) == 0 then
    redis.call('HSET', key,
        'state', 'CLOSED',
        'failure_count', 0,
        'success_count', 0,
        'last_failure_time', '',
        'half_open_in_flight', 0,
        'half_open_claimed_at', 0)
    redis.call('EXPIRE', key, ttl)
end

-- Increment failure count and record time
local new_count = redis.call('HINCRBY', key, 'failure_count', 1)
redis.call('HSET', key, 'last_failure_time', now)

local state = redis.call('HGET', key, 'state')

-- Fail-safe: ANY failure during HALF_OPEN reopens the circuit immediately.
-- We do not track per-process slot ownership. If any process reports a failure
-- while the circuit is testing (HALF_OPEN), the service is still unhealthy.
-- This is intentional: deny > allow.
if state == 'HALF_OPEN' then
    redis.call('HSET', key, 'state', 'OPEN', 'half_open_in_flight', 0, 'half_open_claimed_at', 0)
elseif new_count >= threshold then
    redis.call('HSET', key, 'state', 'OPEN')
end

redis.call('EXPIRE', key, ttl)
return new_count
"""

# record_success Lua script:
# KEYS[1] = hash key
# ARGV[1] = ttl_seconds (int)
# Returns: new success_count (int)
_LUA_RECORD_SUCCESS = """
local key = KEYS[1]
local ttl = tonumber(ARGV[1])

-- Initialize fields if missing
if redis.call('EXISTS', key) == 0 then
    redis.call('HSET', key,
        'state', 'CLOSED',
        'failure_count', 0,
        'success_count', 0,
        'last_failure_time', '',
        'half_open_in_flight', 0,
        'half_open_claimed_at', 0)
    redis.call('EXPIRE', key, ttl)
end

local state = redis.call('HGET', key, 'state')

-- Fail-safe: ANY success during HALF_OPEN closes the circuit.
-- Same rationale as _LUA_RECORD_FAILURE: no per-process ownership tracking.
-- If any process succeeds while testing, the service is healthy enough to close.
if state == 'HALF_OPEN' then
    redis.call('HSET', key, 'state', 'CLOSED', 'failure_count', 0,
               'half_open_in_flight', 0, 'half_open_claimed_at', 0)
elseif state == 'CLOSED' then
    redis.call('HSET', key, 'failure_count', 0)
end

local new_count = redis.call('HINCRBY', key, 'success_count', 1)
redis.call('EXPIRE', key, ttl)
return new_count
"""

# check Lua script:
# KEYS[1] = hash key
# ARGV[1] = recovery_timeout (float seconds)
# ARGV[2] = current_time (float, Unix timestamp)
# ARGV[3] = ttl_seconds (int)
# ARGV[4] = half_open_slot_timeout (float seconds, 0 = no timeout)
# Returns: table [state_str, slot_claimed, failure_count]
#   slot_claimed = 1 if we successfully claimed the HALF_OPEN slot (old in_flight was 0)
#               = 0 if slot was already taken by another caller
#               = 0 if state is OPEN or CLOSED (not relevant)
_LUA_CHECK = """
local key = KEYS[1]
local recovery_timeout = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local half_open_slot_timeout = tonumber(ARGV[4])

-- Initialize fields if missing
if redis.call('EXISTS', key) == 0 then
    redis.call('HSET', key,
        'state', 'CLOSED',
        'failure_count', 0,
        'success_count', 0,
        'last_failure_time', '',
        'half_open_in_flight', 0,
        'half_open_claimed_at', 0)
    redis.call('EXPIRE', key, ttl)
    return {'CLOSED', 0, 0}
end

local state = redis.call('HGET', key, 'state')
local last_failure_time_str = redis.call('HGET', key, 'last_failure_time')
local half_open_in_flight = tonumber(redis.call('HGET', key, 'half_open_in_flight') or '0')
local failure_count = tonumber(redis.call('HGET', key, 'failure_count') or '0')

-- Attempt OPEN -> HALF_OPEN transition if recovery timeout elapsed
if state == 'OPEN' and last_failure_time_str ~= nil and last_failure_time_str ~= '' then
    local last_failure_time = tonumber(last_failure_time_str)
    if last_failure_time ~= nil and (now - last_failure_time) >= recovery_timeout then
        state = 'HALF_OPEN'
        redis.call('HSET', key, 'state', 'HALF_OPEN', 'half_open_in_flight', 0,
                   'half_open_claimed_at', 0)
        half_open_in_flight = 0
    end
end

-- For HALF_OPEN: atomically claim the in-flight slot.
-- Return slot_claimed=1 if WE claimed it (old value was 0), 0 if already taken.
local slot_claimed = 0
if state == 'HALF_OPEN' then
    local old_in_flight = half_open_in_flight

    -- Auto-release stale slot if half_open_slot_timeout is configured and elapsed.
    -- This prevents permanent lock-out when the claiming process crashes.
    if old_in_flight == 1 and half_open_slot_timeout > 0 then
        local claimed_at = tonumber(redis.call('HGET', key, 'half_open_claimed_at') or '0')
        if claimed_at > 0 and (now - claimed_at) >= half_open_slot_timeout then
            old_in_flight = 0
            redis.call('HSET', key, 'half_open_in_flight', 0, 'half_open_claimed_at', 0)
        end
    end

    if old_in_flight == 0 then
        redis.call('HSET', key, 'half_open_in_flight', 1, 'half_open_claimed_at', now)
        slot_claimed = 1
    end
end

redis.call('EXPIRE', key, ttl)
return {state, slot_claimed, failure_count}
"""


@dataclasses.dataclass(frozen=True)
class CircuitSnapshot:
    """Immutable snapshot of all circuit breaker state in a single read."""

    state: CircuitState
    failure_count: int
    success_count: int
    last_failure_time: Optional[float]
    distributed: bool
    circuit_id: str


class DistributedCircuitBreaker:
    """Redis-backed distributed circuit breaker for cross-process failure isolation.

    Shares circuit state (CLOSED/OPEN/HALF_OPEN) across multiple processes via
    a Redis hash. Uses Lua scripts for atomic state transitions to prevent
    race conditions.

    Falls back to a local CircuitBreaker if Redis is unreachable, following
    the same pattern as RedisBudgetBackend.

    HALF_OPEN Slot Semantics:
        When the circuit transitions to HALF_OPEN, exactly one process may claim
        the test slot. If the claiming process crashes without calling
        record_success() or record_failure(), the slot is automatically released
        after ``half_open_slot_timeout`` seconds (default 120s).  Set to 0 to
        disable the timeout (slot persists until TTL expiry or manual reset()).

    Args:
        redis_url: Redis connection URL.
        circuit_id: Unique identifier used as Redis key suffix.
        failure_threshold: Consecutive failures before opening the circuit.
        recovery_timeout: Seconds in OPEN state before trying HALF_OPEN.
        ttl_seconds: Redis key TTL (auto-expire stale circuits).
        fallback_on_error: Fall back to local CircuitBreaker on Redis failure.
        half_open_slot_timeout: Seconds before an unclaimed HALF_OPEN slot is
            auto-released.  Prevents permanent lock-out from process crashes.
            Recommended: ``2 * max_llm_call_timeout``.  0 = no timeout.
        redis_client: Optional pre-created ``redis.Redis`` instance for
            connection pool sharing across multiple breakers.

    Example::

        breaker = DistributedCircuitBreaker(
            redis_url="redis://localhost:6379",
            circuit_id="my-llm-service",
            failure_threshold=5,
            recovery_timeout=60.0,
        )
        decision = breaker.check(PolicyContext())
        if decision.allowed:
            try:
                result = call_llm()
                breaker.record_success()
            except Exception:
                breaker.record_failure()
    """

    KEY_PREFIX = "veronica:circuit:"

    # Minimum seconds between reconnect attempts (prevents hot-loop log storms).
    _RECONNECT_INTERVAL: float = 5.0

    def __init__(
        self,
        redis_url: str,
        circuit_id: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        ttl_seconds: int = 3600,
        fallback_on_error: bool = True,
        half_open_slot_timeout: float = 120.0,
        redis_client: object = None,
        failure_predicate: Optional[FailurePredicate] = None,
    ) -> None:
        self._redis_url = redis_url
        self._circuit_id = circuit_id
        self._key = f"{self.KEY_PREFIX}{circuit_id}"
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._ttl = ttl_seconds
        self._fallback_on_error = fallback_on_error
        self._half_open_slot_timeout = half_open_slot_timeout
        self._failure_predicate = failure_predicate
        self._fallback = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            failure_predicate=failure_predicate,
        )
        self._using_fallback = False
        self._client = None
        self._owns_client = redis_client is None
        self._lock = threading.Lock()
        self._last_reconnect_attempt: float = 0.0
        # Compiled Lua scripts (registered after connect)
        self._script_failure = None
        self._script_success = None
        self._script_check = None
        if redis_client is not None:
            self._client = redis_client
            self._register_scripts()
        else:
            self._connect()

    def _register_scripts(self) -> None:
        """Register Lua scripts on the current Redis client."""
        self._script_failure = self._client.register_script(_LUA_RECORD_FAILURE)
        self._script_success = self._client.register_script(_LUA_RECORD_SUCCESS)
        self._script_check = self._client.register_script(_LUA_CHECK)

    def _connect(self) -> None:
        """Connect to Redis and register Lua scripts."""
        try:
            import redis

            self._client = redis.from_url(self._redis_url, decode_responses=True)
            self._client.ping()
            self._register_scripts()
            self._using_fallback = False
        except Exception as exc:
            if self._fallback_on_error:
                logger.warning(
                    "DistributedCircuitBreaker: cannot connect to Redis (%s). "
                    "Falling back to local CircuitBreaker.",
                    exc,
                )
                self._using_fallback = True
            else:
                raise

    def _seed_fallback_from_redis(self) -> None:
        """Read current Redis state and sync the local fallback CircuitBreaker.

        Called while Redis is still live (just before transition to fallback).
        Safe to skip on error — fallback starts in CLOSED state which is
        permissive but not crash-inducing.
        """
        try:
            data = self._client.hgetall(self._key)
            if not data:
                return
            state_str = data.get("state", "CLOSED")
            failure_count = int(data.get("failure_count", 0))
            last_failure_time_str = data.get("last_failure_time", "")
            last_failure_time = (
                float(last_failure_time_str) if last_failure_time_str else None
            )

            # Mirror state into local fallback
            with self._fallback._lock:
                self._fallback._failure_count = failure_count
                self._fallback._last_failure_time = last_failure_time
                self._fallback._success_count = int(data.get("success_count", 0))
                self._fallback._half_open_in_flight = 0
                try:
                    self._fallback._state = CircuitState(state_str)
                except ValueError:
                    self._fallback._state = CircuitState.CLOSED

            logger.info(
                "DistributedCircuitBreaker: seeded local fallback from Redis "
                "(state=%s, failures=%d).",
                state_str,
                failure_count,
            )
        except Exception as exc:
            logger.warning(
                "DistributedCircuitBreaker: could not seed fallback from Redis (%s) — "
                "fallback starts in CLOSED state.",
                exc,
            )

    def _reconcile_on_reconnect(self) -> bool:
        """Push local fallback state back to Redis after reconnection.

        Writes the local CircuitBreaker state atomically to the Redis hash
        so that other processes benefit from the local failure history
        accumulated during the outage.

        Returns:
            True if reconciliation succeeded, False otherwise.
        """
        try:
            with self._fallback._lock:
                state_str = self._fallback._state.value
                failure_count = self._fallback._failure_count
                success_count = self._fallback._success_count
                last_failure_time = self._fallback._last_failure_time

            pipe = self._client.pipeline()
            pipe.hset(
                self._key,
                mapping={
                    "state": state_str,
                    "failure_count": failure_count,
                    "success_count": success_count,
                    "last_failure_time": (
                        last_failure_time if last_failure_time is not None else ""
                    ),
                    "half_open_in_flight": 0,
                    "half_open_claimed_at": 0,
                },
            )
            pipe.expire(self._key, self._ttl)
            pipe.execute()
            # Re-register scripts after reconnect
            self._register_scripts()
            logger.info(
                "DistributedCircuitBreaker: reconciled local state to Redis "
                "(state=%s, failures=%d).",
                state_str,
                failure_count,
            )
            return True
        except Exception as exc:
            logger.error(
                "DistributedCircuitBreaker: reconciliation failed (%s) — "
                "fallback state preserved.",
                exc,
            )
            return False

    def _try_reconnect(self) -> bool:
        """Attempt to reconnect to Redis. Returns True if successful.

        Rate-limited via _last_reconnect_attempt to avoid hot-loop log storms.
        Only clears _using_fallback after a successful reconciliation.
        """
        now = time.monotonic()
        if now - self._last_reconnect_attempt < self._RECONNECT_INTERVAL:
            return False
        self._last_reconnect_attempt = now

        try:
            self._connect()
        except Exception:
            return False

        if self._using_fallback:
            return False

        # Connected — stay on fallback until reconcile succeeds.
        self._using_fallback = True
        reconciled = self._reconcile_on_reconnect()
        if reconciled:
            self._using_fallback = False
            logger.info("DistributedCircuitBreaker: reconnected to Redis successfully.")
            return True
        # Reconcile failed — stay on fallback.
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_state_str(self, state_str: str, last_failure_time: Optional[float]) -> CircuitState:
        """Parse state string, applying OPEN->HALF_OPEN timeout if appropriate."""
        if state_str == "OPEN" and last_failure_time is not None:
            if time.time() - last_failure_time >= self._recovery_timeout:
                state_str = "HALF_OPEN"
        try:
            return CircuitState(state_str)
        except ValueError:
            return CircuitState.CLOSED

    @staticmethod
    def _parse_last_failure_time(raw: str) -> Optional[float]:
        """Parse last_failure_time from Redis string, returning None on garbage."""
        if not raw:
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Public API (drop-in for CircuitBreaker)
    # ------------------------------------------------------------------

    @property
    def policy_type(self) -> str:
        """RuntimePolicy protocol: policy type identifier."""
        return "circuit_breaker"

    def bind_to_context(self, ctx_id: str) -> None:
        """No-op for distributed breaker — multiple contexts share this instance."""
        pass

    @property
    def state(self) -> CircuitState:
        """Current circuit state (reads from Redis or fallback).

        Uses HMGET to fetch only ``state`` and ``last_failure_time`` (2 fields)
        instead of HGETALL (all fields), reducing data transfer.
        """
        if self._using_fallback or self._client is None:
            return self._fallback.state
        try:
            vals = self._client.hmget(self._key, "state", "last_failure_time")
            state_str = vals[0] or "CLOSED"
            lft = self._parse_last_failure_time(vals[1] or "")
            return self._resolve_state_str(state_str, lft)
        except Exception as exc:
            if self._fallback_on_error:
                logger.error(
                    "DistributedCircuitBreaker.state read failed: %s", exc
                )
                return self._fallback.state
            raise

    @property
    def failure_count(self) -> int:
        """Consecutive failure count (reads from Redis or fallback)."""
        if self._using_fallback or self._client is None:
            return self._fallback.failure_count
        try:
            val = self._client.hget(self._key, "failure_count")
            return int(val) if val is not None else 0
        except Exception as exc:
            if self._fallback_on_error:
                logger.error(
                    "DistributedCircuitBreaker.failure_count read failed: %s", exc
                )
                return self._fallback.failure_count
            raise

    @property
    def success_count(self) -> int:
        """Total success count (reads from Redis or fallback)."""
        if self._using_fallback or self._client is None:
            return self._fallback.success_count
        try:
            val = self._client.hget(self._key, "success_count")
            return int(val) if val is not None else 0
        except Exception as exc:
            if self._fallback_on_error:
                logger.error(
                    "DistributedCircuitBreaker.success_count read failed: %s", exc
                )
                return self._fallback.success_count
            raise

    def check(self, context: PolicyContext) -> PolicyDecision:
        """RuntimePolicy protocol: check if circuit allows the operation.

        Atomically reads state, handles OPEN->HALF_OPEN timeout transition,
        and claims the half-open in-flight slot via Lua script.

        Args:
            context: PolicyContext (fields unused by circuit breaker)

        Returns:
            PolicyDecision allowing (CLOSED/HALF_OPEN first request) or denying (OPEN)
        """
        # Attempt reconnect if on fallback
        if self._using_fallback and self._fallback_on_error:
            with self._lock:
                if self._using_fallback:
                    self._try_reconnect()

        if self._using_fallback or self._client is None:
            return self._fallback.check(context)

        try:
            result = self._script_check(
                keys=[self._key],
                args=[
                    self._recovery_timeout,
                    time.time(),
                    self._ttl,
                    self._half_open_slot_timeout,
                ],
            )
            state_str = result[0]
            slot_claimed = int(result[1])
            failure_count = int(result[2])

            if state_str == "OPEN":
                return PolicyDecision(
                    allowed=False,
                    policy_type=self.policy_type,
                    reason=(
                        f"Circuit OPEN: {failure_count} consecutive failures"
                    ),
                )

            if state_str == "HALF_OPEN":
                # slot_claimed=1 means WE atomically claimed the HALF_OPEN slot
                # (old in_flight was 0, Lua set it to 1 for us).
                # slot_claimed=0 means another caller already holds the slot.
                if slot_claimed == 0:
                    return PolicyDecision(
                        allowed=False,
                        policy_type=self.policy_type,
                        reason="Circuit HALF_OPEN: test request already in flight",
                    )

            return PolicyDecision(allowed=True, policy_type=self.policy_type)

        except Exception as exc:
            if self._fallback_on_error:
                logger.error(
                    "DistributedCircuitBreaker.check failed: %s — using local fallback",
                    exc,
                )
                with self._lock:
                    if not self._using_fallback:
                        self._seed_fallback_from_redis()
                        self._using_fallback = True
                return self._fallback.check(context)
            raise

    def record_success(self) -> None:
        """Record a successful operation.

        Closes the circuit if currently half-open. Resets failure counter.
        """
        # Attempt reconnect if on fallback
        if self._using_fallback and self._fallback_on_error:
            with self._lock:
                if self._using_fallback:
                    self._try_reconnect()

        if self._using_fallback or self._client is None:
            self._fallback.record_success()
            return
        try:
            self._script_success(
                keys=[self._key],
                args=[self._ttl],
            )
            logger.debug(
                "[VERONICA_CIRCUIT] DistributedCircuitBreaker: success recorded "
                "(circuit_id=%s)",
                self._circuit_id,
            )
        except Exception as exc:
            if self._fallback_on_error:
                logger.error(
                    "DistributedCircuitBreaker.record_success failed: %s — "
                    "using local fallback",
                    exc,
                )
                with self._lock:
                    if not self._using_fallback:
                        self._seed_fallback_from_redis()
                        self._using_fallback = True
                self._fallback.record_success()
            else:
                raise

    def record_failure(self, *, error: Optional[BaseException] = None) -> bool:
        """Record a failed operation.

        If a ``failure_predicate`` is configured and *error* is provided, the
        predicate is evaluated first. If it returns ``False``, the failure is
        ignored (not counted toward the threshold).  Zero Redis overhead for
        filtered failures.

        Args:
            error: The exception that caused the failure.  When ``None``,
                the failure is always counted (backward compatible with
                callers that have no exception, e.g. AG2 null-reply detection).

        Returns:
            ``True`` if the failure was counted, ``False`` if filtered.
        """
        # Predicate evaluation BEFORE any Redis call (zero overhead for filtered).
        if error is not None and self._failure_predicate is not None:
            try:
                if not self._failure_predicate(error):
                    logger.debug(
                        "[VERONICA_CIRCUIT] DistributedCircuitBreaker: failure "
                        "filtered by predicate (circuit_id=%s, error=%s)",
                        self._circuit_id,
                        type(error).__name__,
                    )
                    return False
            except Exception:
                logger.warning(
                    "[VERONICA_CIRCUIT] DistributedCircuitBreaker: "
                    "failure_predicate raised; counting failure as fail-safe"
                )

        # Attempt reconnect if on fallback
        if self._using_fallback and self._fallback_on_error:
            with self._lock:
                if self._using_fallback:
                    self._try_reconnect()

        if self._using_fallback or self._client is None:
            # Predicate already evaluated; pass without error to avoid
            # double-evaluation in the local fallback.
            self._fallback.record_failure()
            return True
        try:
            new_count = self._script_failure(
                keys=[self._key],
                args=[
                    self._failure_threshold,
                    time.time(),
                    self._ttl,
                ],
            )
            if int(new_count) >= self._failure_threshold:
                logger.warning(
                    "[VERONICA_CIRCUIT] DistributedCircuitBreaker: circuit opened "
                    "(circuit_id=%s, failures=%d)",
                    self._circuit_id,
                    int(new_count),
                )
        except Exception as exc:
            if self._fallback_on_error:
                logger.error(
                    "DistributedCircuitBreaker.record_failure failed: %s — "
                    "using local fallback",
                    exc,
                )
                with self._lock:
                    if not self._using_fallback:
                        self._seed_fallback_from_redis()
                        self._using_fallback = True
                self._fallback.record_failure()
            else:
                raise
        return True

    def reset(self) -> None:
        """Reset circuit to CLOSED state."""
        if self._using_fallback or self._client is None:
            self._fallback.reset()
            return
        try:
            pipe = self._client.pipeline()
            pipe.hset(
                self._key,
                mapping={
                    "state": "CLOSED",
                    "failure_count": 0,
                    "success_count": 0,
                    "last_failure_time": "",
                    "half_open_in_flight": 0,
                    "half_open_claimed_at": 0,
                },
            )
            pipe.expire(self._key, self._ttl)
            pipe.execute()
            logger.info(
                "[VERONICA_CIRCUIT] DistributedCircuitBreaker: reset (circuit_id=%s)",
                self._circuit_id,
            )
        except Exception as exc:
            if self._fallback_on_error:
                logger.error(
                    "DistributedCircuitBreaker.reset failed: %s", exc
                )
                self._fallback.reset()
            else:
                raise

    def snapshot(self) -> CircuitSnapshot:
        """Retrieve all circuit state in a single Redis round-trip.

        Prefer this over reading ``state``/``failure_count``/``success_count``
        individually when you need multiple fields (avoids N+1 Redis reads).
        """
        if self._using_fallback or self._client is None:
            with self._fallback._lock:
                return CircuitSnapshot(
                    state=self._fallback._state,
                    failure_count=self._fallback._failure_count,
                    success_count=self._fallback._success_count,
                    last_failure_time=self._fallback._last_failure_time,
                    distributed=False,
                    circuit_id=self._circuit_id,
                )
        try:
            data = self._client.hgetall(self._key)
            if not data:
                return CircuitSnapshot(
                    state=CircuitState.CLOSED,
                    failure_count=0,
                    success_count=0,
                    last_failure_time=None,
                    distributed=True,
                    circuit_id=self._circuit_id,
                )
            state_str = data.get("state", "CLOSED")
            last_failure_time = self._parse_last_failure_time(
                data.get("last_failure_time", "")
            )
            state = self._resolve_state_str(state_str, last_failure_time)

            return CircuitSnapshot(
                state=state,
                failure_count=int(data.get("failure_count", 0)),
                success_count=int(data.get("success_count", 0)),
                last_failure_time=last_failure_time,
                distributed=True,
                circuit_id=self._circuit_id,
            )
        except Exception as exc:
            if self._fallback_on_error:
                logger.error("DistributedCircuitBreaker.snapshot failed: %s", exc)
                with self._fallback._lock:
                    return CircuitSnapshot(
                        state=self._fallback._state,
                        failure_count=self._fallback._failure_count,
                        success_count=self._fallback._success_count,
                        last_failure_time=self._fallback._last_failure_time,
                        distributed=False,
                        circuit_id=self._circuit_id,
                    )
            raise

    def to_dict(self) -> Dict:
        """Serialize circuit breaker state.

        Delegates to snapshot() for a single-RTT read, then enriches with
        configuration fields (failure_threshold, recovery_timeout).
        """
        snap = self.snapshot()
        return {
            "state": snap.state.value,
            "failure_count": snap.failure_count,
            "failure_threshold": self._failure_threshold,
            "recovery_timeout": self._recovery_timeout,
            "last_failure_time": snap.last_failure_time,
            "success_count": snap.success_count,
            "distributed": snap.distributed,
            "circuit_id": snap.circuit_id,
        }

    @property
    def is_using_fallback(self) -> bool:
        """True if currently operating in local fallback mode."""
        return self._using_fallback


def get_default_circuit_breaker(
    redis_url: Optional[str] = None,
    circuit_id: str = "default",
    failure_threshold: int = 5,
    recovery_timeout: float = 60.0,
    ttl_seconds: int = 3600,
    failure_predicate: Optional[FailurePredicate] = None,
) -> "CircuitBreaker | DistributedCircuitBreaker":
    """Factory: returns DistributedCircuitBreaker if redis_url given, else CircuitBreaker.

    Args:
        redis_url: Redis connection URL (e.g., "redis://localhost:6379").
                   If None, returns a local CircuitBreaker.
        circuit_id: Unique identifier for this circuit (used as Redis key suffix).
        failure_threshold: Number of consecutive failures before opening circuit.
        recovery_timeout: Seconds to wait in OPEN state before trying HALF_OPEN.
        ttl_seconds: Redis key TTL in seconds (auto-expire stale circuits).
        failure_predicate: Optional predicate to filter which exceptions count
            as failures.  Receives the exception and returns True to count,
            False to ignore.

    Returns:
        DistributedCircuitBreaker if redis_url is provided, else CircuitBreaker.
    """
    if redis_url:
        return DistributedCircuitBreaker(
            redis_url=redis_url,
            circuit_id=circuit_id,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            ttl_seconds=ttl_seconds,
            failure_predicate=failure_predicate,
        )
    return CircuitBreaker(
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout,
        failure_predicate=failure_predicate,
    )
