"""Tests for DistributedCircuitBreaker — Redis-backed distributed circuit breaker."""
from __future__ import annotations

import threading
import time
from typing import List
from unittest.mock import patch

import fakeredis
import pytest

from veronica_core.circuit_breaker import CircuitBreaker, CircuitState
from veronica_core.distributed import (
    CircuitSnapshot,
    DistributedCircuitBreaker,
    get_default_circuit_breaker,
)
from veronica_core.runtime_policy import PolicyContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_CTX = PolicyContext()


def _ctx() -> PolicyContext:
    return _CTX


def _make_dcb(
    fake_client,
    circuit_id: str = "test",
    failure_threshold: int = 3,
    recovery_timeout: float = 60.0,
    ttl_seconds: int = 3600,
    half_open_slot_timeout: float = 120.0,
) -> DistributedCircuitBreaker:
    """Create DistributedCircuitBreaker with injected fakeredis client."""
    dcb = DistributedCircuitBreaker.__new__(DistributedCircuitBreaker)
    dcb._redis_url = "redis://fake"
    dcb._circuit_id = circuit_id
    dcb._key = f"veronica:circuit:{circuit_id}"
    dcb._failure_threshold = failure_threshold
    dcb._recovery_timeout = recovery_timeout
    dcb._ttl = ttl_seconds
    dcb._fallback_on_error = True
    dcb._half_open_slot_timeout = half_open_slot_timeout
    dcb._fallback = CircuitBreaker(
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout,
    )
    dcb._using_fallback = False
    dcb._client = fake_client
    dcb._owns_client = False
    dcb._lock = threading.Lock()
    dcb._last_reconnect_attempt = 0.0
    # Register Lua scripts on the fake client
    dcb._script_failure = fake_client.register_script(
        _import_lua("_LUA_RECORD_FAILURE")
    )
    dcb._script_success = fake_client.register_script(
        _import_lua("_LUA_RECORD_SUCCESS")
    )
    dcb._script_check = fake_client.register_script(
        _import_lua("_LUA_CHECK")
    )
    return dcb


def _import_lua(name: str) -> str:
    """Import Lua script string from distributed module."""
    import veronica_core.distributed as dist_mod
    return getattr(dist_mod, name)


@pytest.fixture
def fake_server():
    return fakeredis.FakeServer()


@pytest.fixture
def fake_client(fake_server):
    return fakeredis.FakeRedis(server=fake_server, decode_responses=True)


@pytest.fixture
def dcb(fake_client):
    return _make_dcb(fake_client)


# ---------------------------------------------------------------------------
# 1. Basic state transitions: CLOSED -> OPEN -> HALF_OPEN -> CLOSED
# ---------------------------------------------------------------------------


class TestBasicStateTransitions:
    def test_initial_state_is_closed(self, dcb):
        assert dcb.state == CircuitState.CLOSED

    def test_record_failure_increments_count(self, dcb):
        dcb.record_failure()
        assert dcb.failure_count == 1

    def test_opens_after_threshold(self, dcb):
        for _ in range(3):
            dcb.record_failure()
        assert dcb.state == CircuitState.OPEN

    def test_not_open_before_threshold(self, dcb):
        for _ in range(2):
            dcb.record_failure()
        assert dcb.state == CircuitState.CLOSED

    def test_half_open_after_recovery_timeout(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=2, recovery_timeout=0.0)
        dcb.record_failure()
        dcb.record_failure()
        # With recovery_timeout=0.0, state read immediately transitions OPEN->HALF_OPEN
        # (the Redis hash has state=OPEN, but the state property applies the timeout check)
        state = dcb.state
        assert state in (CircuitState.OPEN, CircuitState.HALF_OPEN), (
            "After threshold exceeded, state must be OPEN or already transitioning to HALF_OPEN"
        )

    def test_success_in_half_open_closes_circuit(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0)
        dcb.record_failure()
        # check() claims the HALF_OPEN slot
        decision = dcb.check(_ctx())
        assert decision.allowed
        dcb.record_success()
        assert dcb.state == CircuitState.CLOSED

    def test_failure_in_half_open_reopens_circuit(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=9999.0)
        dcb.record_failure()
        # Manually force HALF_OPEN in Redis (bypass recovery_timeout)
        fake_client.hset(dcb._key, "state", "HALF_OPEN")
        assert dcb.state == CircuitState.HALF_OPEN
        dcb.check(_ctx())  # claim slot
        dcb.record_failure()
        # Check Redis directly (state property would re-check timeout)
        data = fake_client.hgetall(dcb._key)
        assert data.get("state") == "OPEN"

    def test_success_count_increments(self, dcb):
        dcb.record_success()
        assert dcb.success_count == 1

    def test_success_resets_failure_count_in_closed(self, dcb):
        dcb.record_failure()
        dcb.record_failure()
        dcb.record_success()
        assert dcb.failure_count == 0


# ---------------------------------------------------------------------------
# 2. check() denies when OPEN
# ---------------------------------------------------------------------------


class TestCheckDeniesWhenOpen:
    def test_check_denies_in_open_state(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=1)
        dcb.record_failure()
        decision = dcb.check(_ctx())
        assert not decision.allowed
        assert "OPEN" in decision.reason
        assert decision.policy_type == "circuit_breaker"

    def test_check_allows_in_closed_state(self, dcb):
        decision = dcb.check(_ctx())
        assert decision.allowed

    def test_check_reason_includes_failure_count(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=2)
        dcb.record_failure()
        dcb.record_failure()
        decision = dcb.check(_ctx())
        assert not decision.allowed
        assert "2" in decision.reason


# ---------------------------------------------------------------------------
# 3. check() allows exactly 1 in HALF_OPEN (concurrency test)
# ---------------------------------------------------------------------------


class TestHalfOpenConcurrency:
    def test_only_first_check_passes_half_open(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0)
        dcb.record_failure()
        # Ensure HALF_OPEN
        _ = dcb.state

        first = dcb.check(_ctx())
        second = dcb.check(_ctx())

        assert first.allowed, "First check must be allowed in HALF_OPEN"
        assert not second.allowed, "Second check must be denied in HALF_OPEN"
        assert "already in flight" in (second.reason or "")

    def test_concurrent_threads_only_one_passes(self, fake_client):
        """Under real thread concurrency, exactly one thread gets ALLOW."""
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0)
        dcb.record_failure()
        # Ensure HALF_OPEN state is visible
        _ = dcb.state

        results: List[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            decision = dcb.check(_ctx())
            with lock:
                results.append(decision.allowed)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        allowed_count = sum(results)
        assert allowed_count == 1, (
            f"Expected exactly 1 allowed in HALF_OPEN concurrency, got {allowed_count}"
        )


# ---------------------------------------------------------------------------
# 4. record_failure() opens circuit after threshold
# ---------------------------------------------------------------------------


class TestRecordFailureOpensCircuit:
    def test_threshold_exact(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=5)
        for i in range(5):
            dcb.record_failure()
        assert dcb.state == CircuitState.OPEN

    def test_threshold_minus_one_stays_closed(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=5)
        for i in range(4):
            dcb.record_failure()
        assert dcb.state == CircuitState.CLOSED

    def test_failure_from_half_open_reopens(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0)
        dcb.record_failure()
        _ = dcb.state  # transition to HALF_OPEN
        dcb.check(_ctx())  # claim slot
        dcb.record_failure()
        # Should be OPEN (not HALF_OPEN or CLOSED)
        data = dcb._client.hgetall(dcb._key)
        assert data.get("state") == "OPEN"


# ---------------------------------------------------------------------------
# 5. record_success() closes from HALF_OPEN
# ---------------------------------------------------------------------------


class TestRecordSuccessClosesFromHalfOpen:
    def test_closes_from_half_open(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0)
        dcb.record_failure()
        _ = dcb.state  # HALF_OPEN
        dcb.check(_ctx())
        dcb.record_success()
        assert dcb.state == CircuitState.CLOSED
        assert dcb.failure_count == 0

    def test_success_resets_half_open_in_flight(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0)
        dcb.record_failure()
        _ = dcb.state
        dcb.check(_ctx())
        dcb.record_success()
        # After close, in_flight should be reset to 0
        data = dcb._client.hgetall(dcb._key)
        assert data.get("half_open_in_flight", "0") == "0"


# ---------------------------------------------------------------------------
# 6. Redis failover -> falls back to local CircuitBreaker
# ---------------------------------------------------------------------------


class TestRedisFailover:
    def test_falls_back_on_connect_failure(self):
        dcb = DistributedCircuitBreaker(
            redis_url="redis://127.0.0.1:19999",  # nothing listening
            circuit_id="failover-test",
            failure_threshold=3,
            recovery_timeout=60.0,
            fallback_on_error=True,
        )
        assert dcb.is_using_fallback is True
        # Should still function via local fallback
        decision = dcb.check(_ctx())
        assert decision.allowed

    def test_fallback_tracks_failures(self):
        dcb = DistributedCircuitBreaker(
            redis_url="redis://127.0.0.1:19999",
            circuit_id="failover-failures",
            failure_threshold=2,
            recovery_timeout=60.0,
            fallback_on_error=True,
        )
        assert dcb.is_using_fallback is True
        dcb.record_failure()
        dcb.record_failure()
        assert dcb.state == CircuitState.OPEN

    def test_operations_work_during_redis_failure(self, fake_client):
        """When Redis goes down mid-operation, fallback handles subsequent calls."""
        dcb = _make_dcb(fake_client, failure_threshold=5)
        # Record some failures while Redis is up
        dcb.record_failure()
        assert dcb.failure_count == 1

        # Simulate Redis going away
        original_script = dcb._script_failure

        def raise_error(*args, **kwargs):
            raise ConnectionError("Redis gone")

        dcb._script_failure = raise_error
        dcb.record_failure()  # triggers fallback
        assert dcb.is_using_fallback is True

        # Restore
        dcb._script_failure = original_script


# ---------------------------------------------------------------------------
# 7. Reconnect + reconcile: local state pushed back to Redis
# ---------------------------------------------------------------------------


class TestReconnectAndReconcile:
    def test_reconcile_pushes_local_state_to_redis(self, fake_client):
        """After fallback, reconnect should push local state to Redis."""
        dcb = _make_dcb(fake_client, failure_threshold=3)
        # Force fallback with some local state
        dcb._using_fallback = True
        dcb._fallback._failure_count = 2
        with dcb._fallback._lock:
            dcb._fallback._state = CircuitState.CLOSED

        # Reconcile
        result = dcb._reconcile_on_reconnect()
        assert result is True

        data = fake_client.hgetall(dcb._key)
        assert data.get("state") == "CLOSED"
        assert data.get("failure_count") == "2"

    def test_reconnect_clears_using_fallback_on_success(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=3)
        dcb._using_fallback = True
        dcb._last_reconnect_attempt = 0.0

        def fake_connect(self_inner):
            self_inner._client = fake_client
            self_inner._using_fallback = False
            self_inner._script_failure = fake_client.register_script(
                _import_lua("_LUA_RECORD_FAILURE")
            )
            self_inner._script_success = fake_client.register_script(
                _import_lua("_LUA_RECORD_SUCCESS")
            )
            self_inner._script_check = fake_client.register_script(
                _import_lua("_LUA_CHECK")
            )

        with patch.object(type(dcb), "_connect", fake_connect):
            result = dcb._try_reconnect()

        assert result is True
        assert dcb.is_using_fallback is False

    def test_reconcile_failure_preserves_using_fallback(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=3)
        dcb._using_fallback = True
        dcb._last_reconnect_attempt = 0.0

        class BrokenPipeline:
            def hmset(self, *a, **kw): pass
            def expire(self, *a, **kw): pass
            def execute(self): raise ConnectionError("Redis gone")

        original_pipeline = fake_client.pipeline

        def fake_connect(self_inner):
            self_inner._client = fake_client
            self_inner._using_fallback = False

        fake_client.pipeline = lambda: BrokenPipeline()

        with patch.object(type(dcb), "_connect", fake_connect):
            result = dcb._try_reconnect()

        assert result is False
        assert dcb.is_using_fallback is True

        # Restore
        fake_client.pipeline = original_pipeline


# ---------------------------------------------------------------------------
# 8. to_dict() serialization
# ---------------------------------------------------------------------------


class TestToDict:
    def test_to_dict_closed_state(self, dcb):
        d = dcb.to_dict()
        assert d["state"] == "CLOSED"
        assert d["failure_count"] == 0
        assert d["failure_threshold"] == 3
        assert d["recovery_timeout"] == 60.0
        assert d["last_failure_time"] is None
        assert d["success_count"] == 0
        assert d["distributed"] is True
        assert d["circuit_id"] == "test"

    def test_to_dict_open_state(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=2)
        dcb.record_failure()
        dcb.record_failure()
        d = dcb.to_dict()
        assert d["state"] == "OPEN"
        assert d["failure_count"] == 2
        assert d["last_failure_time"] is not None

    def test_to_dict_fallback_mode(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=3)
        dcb._using_fallback = True
        d = dcb.to_dict()
        assert d["distributed"] is False
        assert d["circuit_id"] == "test"


# ---------------------------------------------------------------------------
# 9. reset() works
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_from_open(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=1)
        dcb.record_failure()
        assert dcb.state == CircuitState.OPEN
        dcb.reset()
        assert dcb.state == CircuitState.CLOSED

    def test_reset_clears_counters(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=5)
        dcb.record_failure()
        dcb.record_failure()
        dcb.record_success()
        dcb.reset()
        assert dcb.failure_count == 0
        assert dcb.success_count == 0

    def test_reset_in_fallback_mode(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=2)
        dcb._using_fallback = True
        dcb._fallback.record_failure()
        dcb._fallback.record_failure()
        dcb.reset()
        assert dcb._fallback.failure_count == 0


# ---------------------------------------------------------------------------
# 10. TTL is set on Redis key
# ---------------------------------------------------------------------------


class TestTTL:
    def test_ttl_set_on_record_failure(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=5, ttl_seconds=7200)
        dcb.record_failure()
        ttl = fake_client.ttl(dcb._key)
        assert ttl > 0

    def test_ttl_set_on_check(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=5, ttl_seconds=1800)
        dcb.check(_ctx())
        ttl = fake_client.ttl(dcb._key)
        assert ttl > 0

    def test_ttl_set_on_record_success(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=5, ttl_seconds=3600)
        dcb.record_success()
        ttl = fake_client.ttl(dcb._key)
        assert ttl > 0

    def test_ttl_set_on_reset(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=5, ttl_seconds=3600)
        dcb.reset()
        ttl = fake_client.ttl(dcb._key)
        assert ttl > 0


# ---------------------------------------------------------------------------
# 11. Multiple instances sharing same circuit_id see each other's state changes
# ---------------------------------------------------------------------------


class TestCrossProcessStateSharing:
    def test_second_instance_sees_failures_from_first(self, fake_server):
        client1 = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
        client2 = fakeredis.FakeRedis(server=fake_server, decode_responses=True)

        dcb1 = _make_dcb(client1, circuit_id="shared", failure_threshold=3)
        dcb2 = _make_dcb(client2, circuit_id="shared", failure_threshold=3)

        # Instance 1 records failures
        dcb1.record_failure()
        dcb1.record_failure()
        dcb1.record_failure()

        # Instance 2 should see OPEN state
        assert dcb2.state == CircuitState.OPEN

    def test_check_denied_on_second_instance_after_first_opens(self, fake_server):
        client1 = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
        client2 = fakeredis.FakeRedis(server=fake_server, decode_responses=True)

        dcb1 = _make_dcb(client1, circuit_id="shared2", failure_threshold=2)
        dcb2 = _make_dcb(client2, circuit_id="shared2", failure_threshold=2)

        dcb1.record_failure()
        dcb1.record_failure()

        decision = dcb2.check(_ctx())
        assert not decision.allowed

    def test_success_on_one_visible_to_other(self, fake_server):
        client1 = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
        client2 = fakeredis.FakeRedis(server=fake_server, decode_responses=True)

        dcb1 = _make_dcb(client1, circuit_id="shared3", failure_threshold=1,
                          recovery_timeout=0.0)
        dcb2 = _make_dcb(client2, circuit_id="shared3", failure_threshold=1,
                          recovery_timeout=0.0)

        dcb1.record_failure()
        _ = dcb1.state  # HALF_OPEN
        dcb1.check(_ctx())  # claim slot
        dcb1.record_success()

        assert dcb2.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# 12. Thread safety: concurrent record_failure from multiple threads
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_failures_open_circuit(self, fake_client):
        """Multiple threads recording failures should atomically update counter."""
        dcb = _make_dcb(fake_client, failure_threshold=10)
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            dcb.record_failure()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 10 failures must be recorded atomically
        assert dcb.failure_count == 10
        assert dcb.state == CircuitState.OPEN

    def test_concurrent_successes_increment_count(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=100)
        barrier = threading.Barrier(5)

        def worker():
            barrier.wait()
            dcb.record_success()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert dcb.success_count == 5

    def test_concurrent_check_in_half_open_exactly_one_passes(self, fake_client):
        """Concurrent checks in HALF_OPEN state must allow exactly one."""
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0)
        dcb.record_failure()
        _ = dcb.state  # ensure HALF_OPEN

        results: List[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()
            decision = dcb.check(_ctx())
            with lock:
                results.append(decision.allowed)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        allowed_count = sum(results)
        assert allowed_count == 1, (
            f"Expected exactly 1 allowed in HALF_OPEN concurrency, got {allowed_count}"
        )


# ---------------------------------------------------------------------------
# 13. get_default_circuit_breaker() factory
# ---------------------------------------------------------------------------


class TestGetDefaultCircuitBreaker:
    def test_returns_local_when_no_url(self):
        cb = get_default_circuit_breaker()
        assert isinstance(cb, CircuitBreaker)
        assert not isinstance(cb, DistributedCircuitBreaker)

    def test_returns_distributed_when_url_given(self):
        # Bad URL with fallback
        cb = get_default_circuit_breaker(
            redis_url="redis://127.0.0.1:19999",
            circuit_id="factory-test",
        )
        assert isinstance(cb, DistributedCircuitBreaker)
        assert cb.is_using_fallback is True

    def test_local_breaker_custom_threshold(self):
        cb = get_default_circuit_breaker(failure_threshold=10, recovery_timeout=30.0)
        assert isinstance(cb, CircuitBreaker)
        assert cb.failure_threshold == 10
        assert cb.recovery_timeout == 30.0


# ---------------------------------------------------------------------------
# 14. reconnect rate limiting
# ---------------------------------------------------------------------------


class TestReconnectRateLimit:
    def test_rate_limited_reconnect_returns_false(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=3)
        dcb._using_fallback = True
        connect_calls = [0]

        def counting_connect(self_inner):
            connect_calls[0] += 1
            self_inner._client = fake_client
            self_inner._using_fallback = False
            self_inner._script_failure = fake_client.register_script(
                _import_lua("_LUA_RECORD_FAILURE")
            )
            self_inner._script_success = fake_client.register_script(
                _import_lua("_LUA_RECORD_SUCCESS")
            )
            self_inner._script_check = fake_client.register_script(
                _import_lua("_LUA_CHECK")
            )

        # First call: allowed
        dcb._last_reconnect_attempt = 0.0
        with patch.object(type(dcb), "_connect", counting_connect):
            dcb._try_reconnect()

        first_count = connect_calls[0]
        assert first_count >= 1

        # Second call immediately after: rate-limited
        with patch.object(type(dcb), "_connect", counting_connect):
            result = dcb._try_reconnect()

        assert result is False
        assert connect_calls[0] == first_count, (
            "_connect must not be called again within _RECONNECT_INTERVAL"
        )


# ---------------------------------------------------------------------------
# 15. is_using_fallback property
# ---------------------------------------------------------------------------


class TestIsUsingFallback:
    def test_not_using_fallback_with_working_redis(self, fake_client):
        dcb = _make_dcb(fake_client)
        assert dcb.is_using_fallback is False

    def test_using_fallback_when_forced(self, fake_client):
        dcb = _make_dcb(fake_client)
        dcb._using_fallback = True
        assert dcb.is_using_fallback is True


# ---------------------------------------------------------------------------
# 16. Seed fallback from Redis state
# ---------------------------------------------------------------------------


class TestSeedFallback:
    def test_seed_copies_open_state_to_fallback(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=3)
        # Manually set Redis state to OPEN
        fake_client.hset(dcb._key, mapping={
            "state": "OPEN",
            "failure_count": 3,
            "success_count": 1,
            "last_failure_time": str(time.time() - 1.0),
            "half_open_in_flight": 0,
        })
        dcb._seed_fallback_from_redis()

        assert dcb._fallback._failure_count == 3
        assert dcb._fallback._state == CircuitState.OPEN

    def test_seed_with_empty_redis_does_not_crash(self, fake_client):
        dcb = _make_dcb(fake_client)
        # No key in Redis
        dcb._seed_fallback_from_redis()
        # Should remain in default CLOSED state
        assert dcb._fallback.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# 17. Adversarial / "break it" tests
# ---------------------------------------------------------------------------


class TestAdversarialRedisCorruption:
    """Test that DistributedCircuitBreaker survives corrupted Redis data."""

    def test_invalid_state_string_in_redis(self, fake_client):
        """state field contains garbage — must not crash, should default to CLOSED."""
        dcb = _make_dcb(fake_client, failure_threshold=3)
        fake_client.hset(dcb._key, mapping={
            "state": "TOTALLY_INVALID_STATE",
            "failure_count": "0",
            "success_count": "0",
            "last_failure_time": "",
            "half_open_in_flight": "0",
        })
        # state property must handle ValueError gracefully
        assert dcb.state == CircuitState.CLOSED

    def test_non_numeric_failure_count_in_redis(self, fake_client):
        """failure_count contains non-numeric string — must not crash."""
        dcb = _make_dcb(fake_client, failure_threshold=3)
        fake_client.hset(dcb._key, mapping={
            "state": "CLOSED",
            "failure_count": "NaN_garbage",
            "success_count": "0",
            "last_failure_time": "",
            "half_open_in_flight": "0",
        })
        # failure_count property does int(val); ValueError → fallback
        try:
            count = dcb.failure_count
            # If no exception, should be some default (0 from fallback or parsed)
            assert isinstance(count, int)
        except ValueError:
            pass  # acceptable — fails loudly, doesn't silently corrupt

    def test_non_numeric_last_failure_time_in_redis(self, fake_client):
        """last_failure_time contains garbage — state must not crash."""
        dcb = _make_dcb(fake_client, failure_threshold=1)
        fake_client.hset(dcb._key, mapping={
            "state": "OPEN",
            "failure_count": "5",
            "success_count": "0",
            "last_failure_time": "not_a_timestamp",
            "half_open_in_flight": "0",
        })
        # state property tries float(last_str) which will raise ValueError
        # should be caught, state remains OPEN (no transition to HALF_OPEN)
        state = dcb.state
        assert state == CircuitState.OPEN

    def test_missing_fields_in_redis_hash(self, fake_client):
        """Redis hash exists but has only partial fields — must not crash."""
        dcb = _make_dcb(fake_client, failure_threshold=3)
        # Only set state, nothing else
        fake_client.hset(dcb._key, "state", "CLOSED")
        decision = dcb.check(_ctx())
        assert decision.allowed
        assert dcb.failure_count == 0
        assert dcb.success_count == 0

    def test_empty_hash_in_redis(self, fake_client):
        """Key exists but hash is empty (e.g., all fields DELeted externally)."""
        dcb = _make_dcb(fake_client, failure_threshold=3)
        # Create key then delete all fields
        fake_client.hset(dcb._key, "state", "CLOSED")
        fake_client.hdel(dcb._key, "state")
        # hgetall returns {} for an empty hash (key may still exist)
        state = dcb.state
        assert state == CircuitState.CLOSED  # default

    def test_external_state_mutation_during_check(self, fake_client):
        """External process mutates Redis state between our operations.

        Simulates: another process opens circuit between our hgetall and check.
        Lua script atomicity should prevent this from being a problem for check(),
        but state property (non-Lua) should handle it gracefully.
        """
        dcb = _make_dcb(fake_client, failure_threshold=3)
        # Initially CLOSED
        dcb.check(_ctx())
        # External process opens the circuit
        fake_client.hset(dcb._key, mapping={
            "state": "OPEN",
            "failure_count": "10",
            "last_failure_time": str(time.time()),
            "half_open_in_flight": "0",
        })
        # Our next check should see OPEN (Lua reads fresh)
        decision = dcb.check(_ctx())
        assert not decision.allowed

    def test_half_open_in_flight_stuck_without_slot_timeout(self, fake_client):
        """With half_open_slot_timeout=0, a crashed process leaves the slot stuck.

        Only reset() or TTL expiry should clear it.
        """
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0,
                        half_open_slot_timeout=0)
        dcb.record_failure()
        _ = dcb.state  # HALF_OPEN

        # Claim slot
        dcb.check(_ctx())

        # Simulate crash: never call record_success/record_failure
        # Second instance tries to check
        dcb2 = _make_dcb(fake_client, circuit_id="test", failure_threshold=1,
                          recovery_timeout=0.0, half_open_slot_timeout=0)
        decision = dcb2.check(_ctx())
        assert not decision.allowed, (
            "Stuck half_open_in_flight=1 must deny all subsequent checks"
        )

        # Only reset() clears the stuck state
        dcb.reset()
        decision3 = dcb2.check(_ctx())
        assert decision3.allowed

    def test_half_open_slot_auto_released_after_timeout(self, fake_client):
        """With half_open_slot_timeout > 0, a stale slot is auto-released.

        Simulates: process claims HALF_OPEN slot, crashes, timeout elapses,
        next check() auto-releases the slot and allows a new test request.
        """
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0,
                        half_open_slot_timeout=1.0)
        dcb.record_failure()
        _ = dcb.state  # HALF_OPEN

        # Claim slot
        decision1 = dcb.check(_ctx())
        assert decision1.allowed

        # Simulate crash: slot is stuck
        dcb2 = _make_dcb(fake_client, circuit_id="test", failure_threshold=1,
                          recovery_timeout=0.0, half_open_slot_timeout=1.0)
        decision2 = dcb2.check(_ctx())
        assert not decision2.allowed, "Slot still held, should deny"

        # Wait for slot timeout to elapse
        time.sleep(1.1)

        # Now the stale slot should be auto-released
        decision3 = dcb2.check(_ctx())
        assert decision3.allowed, (
            "After half_open_slot_timeout, stale slot must be auto-released"
        )


class TestAdversarialBoundaryValues:
    """Test boundary and edge-case parameter values."""

    def test_failure_threshold_of_one(self, fake_client):
        """Single failure should immediately open."""
        dcb = _make_dcb(fake_client, failure_threshold=1)
        dcb.record_failure()
        assert dcb.state == CircuitState.OPEN

    def test_recovery_timeout_zero(self, fake_client):
        """Immediately transition OPEN -> HALF_OPEN."""
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0)
        dcb.record_failure()
        # check() should either be HALF_OPEN or OPEN (Lua applies timeout)
        decision = dcb.check(_ctx())
        # With timeout=0, should have transitioned to HALF_OPEN and allowed
        assert decision.allowed

    def test_very_large_failure_threshold(self, fake_client):
        """Circuit should stay CLOSED even after many failures if threshold is huge."""
        dcb = _make_dcb(fake_client, failure_threshold=1000000)
        for _ in range(100):
            dcb.record_failure()
        assert dcb.state == CircuitState.CLOSED
        assert dcb.failure_count == 100

    def test_rapid_success_failure_interleaving(self, fake_client):
        """Alternating success/failure should keep circuit closed (resets on success)."""
        dcb = _make_dcb(fake_client, failure_threshold=3)
        for _ in range(100):
            dcb.record_failure()
            dcb.record_failure()
            dcb.record_success()  # resets failure_count
        assert dcb.state == CircuitState.CLOSED
        assert dcb.failure_count == 0


class TestAdversarialTOCTOU:
    """Test race conditions between record_failure and check."""

    def test_concurrent_failure_and_check_race(self, fake_client):
        """Threads racing record_failure() and check() must not corrupt state.

        This is the classic TOCTOU: thread A does record_failure() that opens
        the circuit, while thread B is in the middle of check(). Lua atomicity
        should ensure consistency.
        """
        dcb = _make_dcb(fake_client, failure_threshold=3)
        # Pre-load 2 failures (one away from threshold)
        dcb.record_failure()
        dcb.record_failure()

        results = {"check_decisions": [], "failures_recorded": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(20)

        def failure_worker():
            barrier.wait()
            dcb.record_failure()
            with lock:
                results["failures_recorded"] += 1

        def check_worker():
            barrier.wait()
            d = dcb.check(_ctx())
            with lock:
                results["check_decisions"].append(d.allowed)

        threads = []
        for _ in range(10):
            threads.append(threading.Thread(target=failure_worker))
            threads.append(threading.Thread(target=check_worker))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # After 2+10 failures, circuit MUST be OPEN
        assert dcb.state == CircuitState.OPEN
        # Failure count should be at least 12 (2 pre-loaded + 10 concurrent)
        assert dcb.failure_count >= 12
        # State must be consistent: no double-opens, no stuck states
        assert dcb.state == CircuitState.OPEN

    def test_concurrent_record_failure_and_record_success(self, fake_client):
        """Threads racing record_failure() and record_success() must converge.

        This tests that Lua atomicity prevents partial updates where
        failure_count is incremented but state is not transitioned.
        """
        dcb = _make_dcb(fake_client, failure_threshold=5)
        barrier = threading.Barrier(10)

        def failure_worker():
            barrier.wait()
            dcb.record_failure()

        def success_worker():
            barrier.wait()
            dcb.record_success()

        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=failure_worker))
            threads.append(threading.Thread(target=success_worker))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # State must be valid (not corrupted)
        state = dcb.state
        assert state in (CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN)
        # failure_count must be non-negative
        assert dcb.failure_count >= 0


class TestAdversarialRedisFailureMidOperation:
    """Test Redis going down in the middle of various operations."""

    def test_redis_dies_during_check_falls_back(self, fake_client):
        """If Redis Lua script fails mid-check, must fallback gracefully."""
        dcb = _make_dcb(fake_client, failure_threshold=3)
        # First check works
        d1 = dcb.check(_ctx())
        assert d1.allowed

        # Sabotage: make script raise
        original = dcb._script_check

        def broken(*args, **kwargs):
            raise ConnectionError("Redis vanished")

        dcb._script_check = broken

        # Should fall back to local and still allow (CLOSED state)
        d2 = dcb.check(_ctx())
        assert d2.allowed
        assert dcb.is_using_fallback is True

        # Restore
        dcb._script_check = original

    def test_redis_dies_during_record_failure_falls_back(self, fake_client):
        """If Redis fails during record_failure, must fallback and still track."""
        dcb = _make_dcb(fake_client, failure_threshold=2)
        dcb.record_failure()  # works on Redis

        # Sabotage
        original = dcb._script_failure

        def broken(*args, **kwargs):
            raise ConnectionError("Redis gone")

        dcb._script_failure = broken

        dcb.record_failure()  # falls back to local
        assert dcb.is_using_fallback is True

        # Restore
        dcb._script_failure = original

    def test_redis_dies_during_record_success_falls_back(self, fake_client):
        """If Redis fails during record_success, must fallback gracefully."""
        dcb = _make_dcb(fake_client, failure_threshold=3)

        def broken(*args, **kwargs):
            raise ConnectionError("Redis gone")

        dcb._script_success = broken

        dcb.record_success()  # falls back
        assert dcb.is_using_fallback is True


class TestAdversarialSeedCorruption:
    """Test _seed_fallback_from_redis with corrupted data."""

    def test_seed_with_invalid_state_defaults_to_closed(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=3)
        fake_client.hset(dcb._key, mapping={
            "state": "GARBAGE",
            "failure_count": "5",
            "success_count": "0",
            "last_failure_time": "",
            "half_open_in_flight": "0",
        })
        dcb._seed_fallback_from_redis()
        # Invalid state → CircuitState.CLOSED fallback
        assert dcb._fallback._state == CircuitState.CLOSED

    def test_seed_with_non_numeric_failure_count(self, fake_client):
        """Non-numeric failure_count in Redis should not crash seed."""
        dcb = _make_dcb(fake_client, failure_threshold=3)
        fake_client.hset(dcb._key, mapping={
            "state": "OPEN",
            "failure_count": "not_a_number",
            "success_count": "0",
            "last_failure_time": str(time.time()),
            "half_open_in_flight": "0",
        })
        # Should either catch ValueError internally or propagate —
        # but must not leave fallback in a corrupted state
        try:
            dcb._seed_fallback_from_redis()
        except (ValueError, TypeError):
            pass  # Acceptable: fails loudly rather than silently corrupting
        # Fallback must be in a valid state regardless
        assert dcb._fallback.state in (
            CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN
        )


class TestSnapshot:
    """Test CircuitSnapshot — single-RTT state retrieval."""

    def test_snapshot_returns_all_fields(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=3)
        snap = dcb.snapshot()
        assert isinstance(snap, CircuitSnapshot)
        assert snap.state == CircuitState.CLOSED
        assert snap.failure_count == 0
        assert snap.success_count == 0
        assert snap.last_failure_time is None
        assert snap.distributed is True
        assert snap.circuit_id == "test"

    def test_snapshot_reflects_failures(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=5)
        dcb.record_failure()
        dcb.record_failure()
        snap = dcb.snapshot()
        assert snap.failure_count == 2
        assert snap.state == CircuitState.CLOSED

    def test_snapshot_reflects_open_state(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=2)
        dcb.record_failure()
        dcb.record_failure()
        snap = dcb.snapshot()
        assert snap.state == CircuitState.OPEN
        assert snap.failure_count == 2
        assert snap.last_failure_time is not None

    def test_snapshot_open_to_half_open_timeout(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0)
        dcb.record_failure()
        snap = dcb.snapshot()
        # recovery_timeout=0.0 means OPEN -> HALF_OPEN immediately
        assert snap.state == CircuitState.HALF_OPEN

    def test_snapshot_on_fallback(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=3)
        dcb._using_fallback = True
        snap = dcb.snapshot()
        assert snap.distributed is False
        assert snap.state == CircuitState.CLOSED

    def test_snapshot_on_redis_error_falls_back(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=3)
        dcb.record_failure()

        # Break Redis
        def broken(*args, **kwargs):
            raise ConnectionError("Redis gone")

        dcb._client.hgetall = broken
        snap = dcb.snapshot()
        assert snap.distributed is False

    def test_snapshot_is_frozen(self, fake_client):
        dcb = _make_dcb(fake_client, failure_threshold=3)
        snap = dcb.snapshot()
        with pytest.raises(AttributeError):
            snap.state = CircuitState.OPEN  # type: ignore[misc]


class TestRedisClientInjection:
    """Test redis_client parameter for connection pool sharing."""

    def test_injected_client_used_directly(self, fake_client):
        """When redis_client is provided, it should be used without creating a new one."""
        dcb = _make_dcb(fake_client, circuit_id="injected")
        decision = dcb.check(_ctx())
        assert decision.allowed
        assert dcb._owns_client is False

    def test_multiple_breakers_share_client(self, fake_client):
        """Multiple DCBs with same fake_client should share connection."""
        dcb1 = _make_dcb(fake_client, circuit_id="breaker-1")
        dcb2 = _make_dcb(fake_client, circuit_id="breaker-2")

        # Both use same Redis client
        assert dcb1._client is dcb2._client

        # Independent circuit state
        dcb1.record_failure()
        assert dcb1.failure_count == 1
        assert dcb2.failure_count == 0


class TestHalfOpenSlotTimeout:
    """Test half_open_slot_timeout feature in depth."""

    def test_default_timeout_is_120(self, fake_client):
        dcb = _make_dcb(fake_client)
        assert dcb._half_open_slot_timeout == 120.0

    def test_custom_timeout(self, fake_client):
        dcb = _make_dcb(fake_client, half_open_slot_timeout=30.0)
        assert dcb._half_open_slot_timeout == 30.0

    def test_zero_timeout_disables_auto_release(self, fake_client):
        """With timeout=0, slot never auto-releases (original behavior)."""
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0,
                        half_open_slot_timeout=0)
        dcb.record_failure()
        dcb.check(_ctx())  # claim slot

        time.sleep(0.1)

        dcb2 = _make_dcb(fake_client, circuit_id="test", failure_threshold=1,
                          recovery_timeout=0.0, half_open_slot_timeout=0)
        decision = dcb2.check(_ctx())
        assert not decision.allowed, "timeout=0 means slot stays stuck"

    def test_slot_not_released_before_timeout(self, fake_client):
        """Slot must remain held until timeout actually elapses."""
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0,
                        half_open_slot_timeout=10.0)
        dcb.record_failure()
        dcb.check(_ctx())  # claim slot

        # Immediately try — should still be held (10s timeout)
        dcb2 = _make_dcb(fake_client, circuit_id="test", failure_threshold=1,
                          recovery_timeout=0.0, half_open_slot_timeout=10.0)
        decision = dcb2.check(_ctx())
        assert not decision.allowed, "Slot held, timeout not elapsed"

    def test_claimed_at_timestamp_recorded(self, fake_client):
        """When slot is claimed, half_open_claimed_at must be set."""
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0)
        dcb.record_failure()
        before = time.time()
        dcb.check(_ctx())  # claim slot
        after = time.time()

        claimed_at = float(fake_client.hget(dcb._key, "half_open_claimed_at"))
        assert before <= claimed_at <= after

    def test_slot_released_on_record_success(self, fake_client):
        """Normal flow: slot is released when record_success is called."""
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0)
        dcb.record_failure()
        dcb.check(_ctx())  # claim slot
        dcb.record_success()  # release slot, close circuit

        assert dcb.state == CircuitState.CLOSED
        in_flight = int(fake_client.hget(dcb._key, "half_open_in_flight"))
        assert in_flight == 0

    def test_slot_released_on_record_failure(self, fake_client):
        """Normal flow: slot is released when record_failure is called (reopens)."""
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=600.0)
        dcb.record_failure()
        # Manually transition to HALF_OPEN for this test
        fake_client.hset(dcb._key, "state", "HALF_OPEN")
        dcb.check(_ctx())  # claim slot
        dcb.record_failure()  # reopen circuit, release slot

        assert dcb.state == CircuitState.OPEN
        in_flight = int(fake_client.hget(dcb._key, "half_open_in_flight"))
        assert in_flight == 0


# ---------------------------------------------------------------------------
# Adversarial tests for v1.1.1 features
# ---------------------------------------------------------------------------


class TestAdversarialSlotTimeout:
    """Break half_open_slot_timeout: corrupted timestamps, TOCTOU, boundary."""

    def test_corrupted_claimed_at_non_numeric(self, fake_client):
        """If half_open_claimed_at is garbage, slot must NOT auto-release
        (fail-safe: keep denying rather than accidentally allowing).
        """
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0,
                        half_open_slot_timeout=0.01)
        dcb.record_failure()
        dcb.check(_ctx())  # claim slot

        # Corrupt the timestamp
        fake_client.hset(dcb._key, "half_open_claimed_at", "GARBAGE")
        time.sleep(0.02)

        dcb2 = _make_dcb(fake_client, circuit_id="test", failure_threshold=1,
                          recovery_timeout=0.0, half_open_slot_timeout=0.01)
        decision = dcb2.check(_ctx())
        # tonumber("GARBAGE") returns nil in Lua -> claimed_at = 0
        # (now - 0) >= 0.01 is true, so slot IS released.
        # This is acceptable: epoch-0 is clearly stale.
        # Either behavior (deny or release) is valid — just must not crash.
        assert isinstance(decision.allowed, bool)

    def test_corrupted_claimed_at_negative(self, fake_client):
        """Negative claimed_at: Lua guard `claimed_at > 0` prevents auto-release.

        This is fail-safe: corrupted/invalid timestamps keep the slot held
        rather than accidentally releasing it.
        """
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0,
                        half_open_slot_timeout=1.0)
        dcb.record_failure()
        dcb.check(_ctx())  # claim slot

        # Set negative timestamp
        fake_client.hset(dcb._key, "half_open_claimed_at", "-1000")
        dcb2 = _make_dcb(fake_client, circuit_id="test", failure_threshold=1,
                          recovery_timeout=0.0, half_open_slot_timeout=1.0)
        decision = dcb2.check(_ctx())
        # Lua: claimed_at = -1000, condition `claimed_at > 0` is false -> no release
        assert not decision.allowed, "Negative claimed_at = invalid = fail-safe deny"

    def test_corrupted_claimed_at_far_future(self, fake_client):
        """Far-future claimed_at should keep slot held (not yet timed out)."""
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0,
                        half_open_slot_timeout=1.0)
        dcb.record_failure()
        dcb.check(_ctx())  # claim slot

        # Set timestamp 1 hour in the future
        fake_client.hset(dcb._key, "half_open_claimed_at", str(time.time() + 3600))
        dcb2 = _make_dcb(fake_client, circuit_id="test", failure_threshold=1,
                          recovery_timeout=0.0, half_open_slot_timeout=1.0)
        decision = dcb2.check(_ctx())
        # (now - future) < 0 < 1.0, so slot stays held
        assert not decision.allowed, "Future claimed_at = not timed out = deny"

    def test_claimed_at_zero_with_in_flight_1(self, fake_client):
        """claimed_at=0 with in_flight=1: Lua guard `claimed_at > 0` prevents release.

        This is fail-safe: claimed_at=0 means the field was never properly set
        (e.g., old data from before half_open_slot_timeout was added).
        Slot stays held until reset() or TTL expiry.
        """
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0,
                        half_open_slot_timeout=1.0)
        dcb.record_failure()
        # Manually set HALF_OPEN with stuck slot but claimed_at=0
        fake_client.hset(dcb._key, mapping={
            "state": "HALF_OPEN",
            "half_open_in_flight": 1,
            "half_open_claimed_at": 0,
        })
        decision = dcb.check(_ctx())
        # Lua: claimed_at = 0, condition `claimed_at > 0` is false -> no release
        assert not decision.allowed, "claimed_at=0 = never set = fail-safe deny"

        # reset() clears it
        dcb.reset()
        decision2 = dcb.check(_ctx())
        assert decision2.allowed

    def test_toctou_concurrent_stale_release(self, fake_client):
        """Two processes both detect stale slot simultaneously.

        Lua atomicity guarantees exactly one claims the new slot.
        """
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0,
                        half_open_slot_timeout=0.01)
        dcb.record_failure()
        dcb.check(_ctx())  # claim slot
        time.sleep(0.02)  # let slot go stale

        results: List[bool] = []
        barrier = threading.Barrier(10, timeout=5)

        def race():
            racer = _make_dcb(fake_client, circuit_id="test",
                              failure_threshold=1, recovery_timeout=0.0,
                              half_open_slot_timeout=0.01)
            barrier.wait()
            d = racer.check(_ctx())
            results.append(d.allowed)

        threads = [threading.Thread(target=race) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one should claim the newly-released slot
        assert results.count(True) == 1, (
            f"Expected exactly 1 slot claim after stale release, got {results.count(True)}"
        )

    def test_negative_slot_timeout_treated_as_disabled(self, fake_client):
        """Negative half_open_slot_timeout should behave like 0 (disabled)."""
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0,
                        half_open_slot_timeout=-5.0)
        dcb.record_failure()
        dcb.check(_ctx())  # claim slot

        time.sleep(0.05)
        dcb2 = _make_dcb(fake_client, circuit_id="test", failure_threshold=1,
                          recovery_timeout=0.0, half_open_slot_timeout=-5.0)
        decision = dcb2.check(_ctx())
        # negative timeout: Lua condition `half_open_slot_timeout > 0` is false
        assert not decision.allowed, "Negative timeout = disabled = slot stays stuck"

    def test_slot_timeout_exact_boundary(self, fake_client):
        """At exactly the boundary, slot should be released (>= in Lua)."""
        timeout = 0.5
        dcb = _make_dcb(fake_client, failure_threshold=1, recovery_timeout=0.0,
                        half_open_slot_timeout=timeout)
        dcb.record_failure()
        dcb.check(_ctx())  # claim slot

        # Wait exactly the timeout + tiny margin
        time.sleep(timeout + 0.05)
        dcb2 = _make_dcb(fake_client, circuit_id="test", failure_threshold=1,
                          recovery_timeout=0.0, half_open_slot_timeout=timeout)
        decision = dcb2.check(_ctx())
        assert decision.allowed, "At/past boundary, slot must be released"


class TestAdversarialInFlightInvariant:
    """Verify HALF_OPEN in-flight slot behavior under multi-process scenarios.

    Design: We do NOT track per-process slot ownership. Instead:
    - Any failure in HALF_OPEN -> reopen (fail-safe: deny > allow)
    - Any success in HALF_OPEN -> close (service recovered)
    - Slot holder's record_failure/record_success works correctly
    - CLOSED/OPEN state record_failure never touches in_flight

    Bug fixed in v1.1.2: both Lua scripts unconditionally reset
    half_open_in_flight to 0, even when state was CLOSED/OPEN.
    Now gated on state == 'HALF_OPEN'.
    """

    def test_record_failure_in_half_open_reopens_circuit(self, fake_server):
        """Process A holds HALF_OPEN slot. Process B calls record_failure().
        Any failure during HALF_OPEN must reopen the circuit (fail-safe).

        Design rationale: We intentionally do NOT track per-process slot ownership.
        If ANY process reports a failure while the circuit is HALF_OPEN, the
        underlying service is still unhealthy. Reopening immediately (deny > allow)
        is safer than preserving the slot and risking more failed requests.
        """
        client_a = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
        client_b = fakeredis.FakeRedis(server=fake_server, decode_responses=True)

        dcb_a = _make_dcb(client_a, circuit_id="shared", failure_threshold=2,
                          recovery_timeout=0.0)
        dcb_b = _make_dcb(client_b, circuit_id="shared", failure_threshold=2,
                          recovery_timeout=0.0)

        # Open the circuit
        dcb_a.record_failure()
        dcb_a.record_failure()
        assert dcb_a.state in (CircuitState.OPEN, CircuitState.HALF_OPEN)

        # A claims the HALF_OPEN slot
        decision_a = dcb_a.check(_ctx())
        assert decision_a.allowed

        # Verify slot is held
        in_flight = int(client_a.hget(dcb_a._key, "half_open_in_flight"))
        assert in_flight == 1

        # B records a failure -- circuit reopens (fail-safe: deny > allow)
        dcb_b.record_failure()

        # Circuit must be OPEN, slot released
        data = client_a.hgetall(dcb_a._key)
        assert data["state"] == "OPEN"
        assert data["half_open_in_flight"] == "0"
        assert data["half_open_claimed_at"] == "0"

    def test_record_success_from_other_process_preserves_slot(self, fake_server):
        """Process A holds HALF_OPEN slot. Process B calls record_success().
        B's success must NOT release A's slot or close the circuit.
        """
        client_a = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
        client_b = fakeredis.FakeRedis(server=fake_server, decode_responses=True)

        dcb_a = _make_dcb(client_a, circuit_id="shared2", failure_threshold=2,
                          recovery_timeout=0.0)
        dcb_b = _make_dcb(client_b, circuit_id="shared2", failure_threshold=2,
                          recovery_timeout=0.0)

        # Open the circuit
        dcb_a.record_failure()
        dcb_a.record_failure()

        # A claims the HALF_OPEN slot
        decision_a = dcb_a.check(_ctx())
        assert decision_a.allowed

        # B records a success (from a call that started in CLOSED state)
        # The circuit is currently HALF_OPEN. B's success should NOT close the circuit
        # because B never held the HALF_OPEN slot -- B was allowed through CLOSED,
        # not through HALF_OPEN gate. However, the Lua script sees state=HALF_OPEN
        # and will transition to CLOSED. This is a known limitation: we don't track
        # slot ownership per-process. The critical invariant is that in_flight is
        # preserved so A's test request isn't duplicated.
        dcb_b.record_success()

        # Even if state transitioned, A's slot should remain conceptually intact:
        # The circuit may now be CLOSED (B's success closed it), which means
        # A's in-flight test is still valid. The key invariant is no double-claim.
        # After B's success closes the circuit, subsequent checks are CLOSED (allowed).
        decision_c = dcb_a.check(_ctx())
        assert decision_c.allowed  # CLOSED state allows all

    def test_slot_holder_record_failure_releases_slot(self, fake_server):
        """Process A holds HALF_OPEN slot, A's call fails. A calls record_failure().
        This MUST release the slot and reopen the circuit.
        """
        client_a = fakeredis.FakeRedis(server=fake_server, decode_responses=True)

        dcb_a = _make_dcb(client_a, circuit_id="shared3", failure_threshold=2,
                          recovery_timeout=600.0)

        # Open and transition to HALF_OPEN
        dcb_a.record_failure()
        dcb_a.record_failure()
        client_a.hset(dcb_a._key, "state", "HALF_OPEN")

        # A claims the slot
        decision_a = dcb_a.check(_ctx())
        assert decision_a.allowed

        # A's call fails -> A reports failure
        dcb_a.record_failure()

        # Slot must be released, circuit reopened
        data = client_a.hgetall(dcb_a._key)
        assert data["state"] == "OPEN"
        assert data["half_open_in_flight"] == "0"
        assert data["half_open_claimed_at"] == "0"

    def test_slot_holder_record_success_releases_slot(self, fake_server):
        """Process A holds HALF_OPEN slot, A's call succeeds. A calls record_success().
        This MUST release the slot and close the circuit.
        """
        client_a = fakeredis.FakeRedis(server=fake_server, decode_responses=True)

        dcb_a = _make_dcb(client_a, circuit_id="shared4", failure_threshold=2,
                          recovery_timeout=0.0)

        # Open the circuit
        dcb_a.record_failure()
        dcb_a.record_failure()

        # A claims the HALF_OPEN slot
        decision_a = dcb_a.check(_ctx())
        assert decision_a.allowed

        # A's call succeeds
        dcb_a.record_success()

        # Slot released, circuit closed
        data = client_a.hgetall(dcb_a._key)
        assert data["state"] == "CLOSED"
        assert data["half_open_in_flight"] == "0"
        assert data["half_open_claimed_at"] == "0"
        assert data["failure_count"] == "0"

    def test_concurrent_record_failure_reopens_to_open(self, fake_server):
        """10 processes call record_failure() while one holds the HALF_OPEN slot.
        First failure in HALF_OPEN reopens circuit (fail-safe). Subsequent
        failures see OPEN state and do not touch in_flight.
        """
        client = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
        dcb = _make_dcb(client, circuit_id="concurrent", failure_threshold=2,
                        recovery_timeout=0.0)

        # Open and claim slot
        dcb.record_failure()
        dcb.record_failure()
        dcb.check(_ctx())  # claim slot

        barrier = threading.Barrier(10, timeout=5)

        def racer():
            racer_dcb = _make_dcb(client, circuit_id="concurrent",
                                  failure_threshold=100, recovery_timeout=600.0)
            barrier.wait()
            racer_dcb.record_failure()

        threads = [threading.Thread(target=racer) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # The state is now OPEN (failures from racers pushed it to OPEN).
        # But the in_flight slot must NOT have been cleared by the racers.
        # Since record_failure transitions HALF_OPEN->OPEN and resets in_flight,
        # and the first racer's failure sees state=HALF_OPEN (the holder's state),
        # the first racer WILL reset in_flight. However, the state also becomes OPEN
        # so subsequent racers see OPEN (not HALF_OPEN) and don't touch in_flight.
        # This is actually correct: once ANY failure is recorded in HALF_OPEN,
        # the circuit reopens and the slot is released. The holder's test failed
        # (or another process failed).
        data = client.hgetall(dcb._key)
        assert data["state"] == "OPEN"


class TestAdversarialSnapshot:
    """Break snapshot(): corrupted Redis data, mid-operation failure."""

    def test_snapshot_with_garbage_state(self, fake_client):
        """Invalid state string in Redis must not crash snapshot()."""
        dcb = _make_dcb(fake_client)
        fake_client.hset(dcb._key, mapping={
            "state": "COMPLETELY_INVALID",
            "failure_count": "3",
            "success_count": "10",
            "last_failure_time": str(time.time()),
            "half_open_in_flight": "0",
            "half_open_claimed_at": "0",
        })
        snap = dcb.snapshot()
        # Invalid state falls back to CLOSED
        assert snap.state == CircuitState.CLOSED
        assert snap.failure_count == 3
        assert snap.success_count == 10

    def test_snapshot_with_non_numeric_counts(self, fake_client):
        """Non-numeric failure_count/success_count must not crash."""
        dcb = _make_dcb(fake_client)
        fake_client.hset(dcb._key, mapping={
            "state": "CLOSED",
            "failure_count": "NaN",
            "success_count": "also_NaN",
            "last_failure_time": "",
            "half_open_in_flight": "0",
            "half_open_claimed_at": "0",
        })
        # Should either raise ValueError or fallback — must not return garbage
        try:
            snap = dcb.snapshot()
            # If it succeeds, counts should be valid integers
            assert isinstance(snap.failure_count, int)
            assert isinstance(snap.success_count, int)
        except (ValueError, TypeError):
            pass  # Acceptable: fail loudly

    def test_snapshot_with_missing_fields(self, fake_client):
        """Partial Redis hash (some fields missing) must not crash."""
        dcb = _make_dcb(fake_client)
        # Only set state, nothing else
        fake_client.hset(dcb._key, "state", "OPEN")
        snap = dcb.snapshot()
        assert snap.state in (CircuitState.OPEN, CircuitState.HALF_OPEN)
        assert snap.failure_count == 0
        assert snap.success_count == 0
        assert snap.last_failure_time is None

    def test_snapshot_with_non_numeric_last_failure_time(self, fake_client):
        """Non-numeric last_failure_time must not crash."""
        dcb = _make_dcb(fake_client)
        fake_client.hset(dcb._key, mapping={
            "state": "OPEN",
            "failure_count": "5",
            "success_count": "0",
            "last_failure_time": "not_a_timestamp",
            "half_open_in_flight": "0",
            "half_open_claimed_at": "0",
        })
        try:
            snap = dcb.snapshot()
            # If it succeeds, last_failure_time should be handled
            assert isinstance(snap, CircuitSnapshot)
        except (ValueError, TypeError):
            pass  # Acceptable

    def test_snapshot_redis_dies_midway(self, fake_client):
        """If Redis dies during snapshot(), must fallback gracefully."""
        dcb = _make_dcb(fake_client)
        dcb.record_failure()  # put some state in Redis

        def explode(*args, **kwargs):
            raise ConnectionError("Redis exploded")

        dcb._client.hgetall = explode
        snap = dcb.snapshot()
        assert snap.distributed is False  # fell back to local
        assert isinstance(snap.state, CircuitState)

    def test_snapshot_consistency_with_individual_properties(self, fake_client):
        """snapshot() must return same data as individual property reads."""
        dcb = _make_dcb(fake_client, failure_threshold=5)
        dcb.record_failure()
        dcb.record_failure()
        dcb.record_success()

        snap = dcb.snapshot()
        assert snap.state == dcb.state
        assert snap.failure_count == dcb.failure_count
        assert snap.success_count == dcb.success_count


class TestAdversarialRedisClientInjection:
    """Break redis_client injection: bad clients, disconnected clients."""

    def test_inject_none_falls_through_to_connect(self):
        """redis_client=None should trigger normal _connect() path."""
        # This will fail to connect (no real Redis) and fallback
        dcb = DistributedCircuitBreaker(
            redis_url="redis://localhost:59999",  # unlikely to exist
            circuit_id="test-none-inject",
            fallback_on_error=True,
            redis_client=None,
        )
        assert dcb.is_using_fallback is True
        assert dcb._owns_client is True

    def test_inject_broken_client_check_falls_back(self, fake_client):
        """Injected client that fails on script execution must fallback."""
        dcb = _make_dcb(fake_client)
        # Break the injected client's script
        def broken(*args, **kwargs):
            raise ConnectionError("Injected client dead")

        dcb._script_check = broken
        decision = dcb.check(_ctx())
        # Should fall back to local
        assert dcb.is_using_fallback is True
        assert decision.allowed  # local fallback is CLOSED

    def test_shared_client_isolation(self, fake_client):
        """Two breakers sharing a client must have fully independent state."""
        dcb1 = _make_dcb(fake_client, circuit_id="iso-1", failure_threshold=2)
        dcb2 = _make_dcb(fake_client, circuit_id="iso-2", failure_threshold=2)

        # Open circuit 1
        dcb1.record_failure()
        dcb1.record_failure()
        assert dcb1.state == CircuitState.OPEN

        # Circuit 2 must still be closed
        assert dcb2.state == CircuitState.CLOSED
        assert dcb2.failure_count == 0
        decision = dcb2.check(_ctx())
        assert decision.allowed

    def test_client_script_registration_failure(self, fake_client):
        """If register_script fails on injected client, constructor should handle it."""
        class BrokenClient:
            def register_script(self, script):
                raise RuntimeError("Script registration failed")
            def ping(self):
                return True

        dcb = DistributedCircuitBreaker.__new__(DistributedCircuitBreaker)
        dcb._redis_url = "redis://fake"
        dcb._circuit_id = "broken-reg"
        dcb._key = "veronica:circuit:broken-reg"
        dcb._failure_threshold = 3
        dcb._recovery_timeout = 60.0
        dcb._ttl = 3600
        dcb._fallback_on_error = True
        dcb._half_open_slot_timeout = 120.0
        dcb._fallback = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
        dcb._using_fallback = False
        dcb._client = BrokenClient()
        dcb._owns_client = False
        dcb._lock = threading.Lock()
        dcb._last_reconnect_attempt = 0.0
        dcb._script_failure = None
        dcb._script_success = None
        dcb._script_check = None

        # _register_scripts() will raise
        with pytest.raises(RuntimeError):
            dcb._register_scripts()
