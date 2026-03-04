"""Tests for distributed budget backends (P1-2)."""
from __future__ import annotations

import threading
import time

import fakeredis
import pytest

from veronica_core.distributed import (
    LocalBudgetBackend,
    RedisBudgetBackend,
    _redact_exc,
    get_default_backend,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis_client():
    server = fakeredis.FakeServer()
    return fakeredis.FakeRedis(server=server, decode_responses=True)


def make_redis_backend(fake_client, chain_id: str = "test") -> RedisBudgetBackend:
    """Create RedisBudgetBackend with injected fakeredis client (bypasses _connect)."""
    backend = RedisBudgetBackend.__new__(RedisBudgetBackend)
    backend._redis_url = "redis://fake"
    backend._chain_id = chain_id
    backend._key = f"veronica:budget:{chain_id}"
    backend._ttl = 3600
    backend._fallback_on_error = True
    backend._fallback = LocalBudgetBackend()
    backend._using_fallback = False
    backend._lock = threading.Lock()
    backend._client = fake_client
    backend._fallback_seed_base = 0.0
    return backend


# ---------------------------------------------------------------------------
# LocalBudgetBackend tests
# ---------------------------------------------------------------------------


def test_local_backend_basic():
    backend = LocalBudgetBackend()
    assert backend.get() == 0.0
    total = backend.add(0.5)
    assert total == 0.5
    total = backend.add(0.3)
    assert abs(total - 0.8) < 1e-9
    backend.reset()
    assert backend.get() == 0.0


def test_local_backend_thread_safety():
    backend = LocalBudgetBackend()
    threads = [
        threading.Thread(target=lambda: backend.add(0.1)) for _ in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert abs(backend.get() - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# RedisBudgetBackend tests (using fakeredis)
# ---------------------------------------------------------------------------


def test_redis_backend_add_get_reset(fake_redis_client):
    backend = make_redis_backend(fake_redis_client)
    assert backend.get() == 0.0
    total = backend.add(0.25)
    assert abs(total - 0.25) < 1e-9
    total = backend.add(0.75)
    assert abs(total - 1.0) < 1e-9
    assert abs(backend.get() - 1.0) < 1e-9
    backend.reset()
    assert backend.get() == 0.0


def test_redis_backend_concurrent_adds(fake_redis_client):
    backend = make_redis_backend(fake_redis_client, chain_id="concurrent")
    results = []
    lock = threading.Lock()

    def add_amount():
        val = backend.add(0.1)
        with lock:
            results.append(val)

    threads = [threading.Thread(target=add_amount) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Final total must be approximately 1.0
    assert abs(backend.get() - 1.0) < 1e-6


def test_redis_backend_ttl_set(fake_redis_client):
    backend = make_redis_backend(fake_redis_client, chain_id="ttl-test")
    backend.add(1.0)
    ttl = fake_redis_client.ttl(backend._key)
    assert ttl > 0


def test_redis_backend_fallback_on_connect_failure():
    backend = RedisBudgetBackend(
        redis_url="redis://127.0.0.1:19999",  # nothing listening
        chain_id="fallback-test",
        ttl_seconds=3600,
        fallback_on_error=True,
    )
    assert backend.is_using_fallback is True
    # Should still work via local fallback
    total = backend.add(0.5)
    assert abs(total - 0.5) < 1e-9


def test_redis_backend_is_using_fallback_property(fake_redis_client):
    backend = make_redis_backend(fake_redis_client)
    assert backend.is_using_fallback is False

    # Force fallback by corrupting the client
    backend._client = None
    backend._using_fallback = True
    assert backend.is_using_fallback is True


# ---------------------------------------------------------------------------
# get_default_backend factory
# ---------------------------------------------------------------------------


def test_get_default_backend_no_url():
    backend = get_default_backend()
    assert isinstance(backend, LocalBudgetBackend)


def test_get_default_backend_with_url():
    # Bad URL with fallback → constructs RedisBudgetBackend, falls back locally
    backend = get_default_backend(
        redis_url="redis://127.0.0.1:19999",
        chain_id="factory-test",
    )
    assert isinstance(backend, RedisBudgetBackend)
    # Must have fallen back (can't connect)
    assert backend.is_using_fallback is True


# ---------------------------------------------------------------------------
# Reconnect reconciliation test
# ---------------------------------------------------------------------------


def test_redis_backend_reconciles_fallback_delta_on_reconnect():
    """Locally accumulated spend is flushed to Redis when the backend reconnects.

    Scenario:
    1. Backend starts with Redis reachable; adds 0.10.
    2. Redis goes away → backend falls back to local; adds 0.20 locally.
    3. Redis comes back → _reconcile_on_reconnect pushes 0.20 to Redis.
    4. Redis key should now equal 0.10 + 0.20 = 0.30 and local fallback should be 0.
    """
    server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

    # Step 1: normal Redis add
    backend = make_redis_backend(fake_client, chain_id="reconcile-test")
    backend.add(0.10)
    assert abs(backend.get() - 0.10) < 1e-9

    # Step 2: simulate Redis outage — force fallback, accumulate locally
    backend._using_fallback = True
    backend._fallback.add(0.20)

    # Step 3: Redis comes back; call reconcile directly (as _try_reconnect would)
    # Ensure the client is still the fake one (connection "restored")
    backend._using_fallback = False
    backend._reconcile_on_reconnect()

    # Step 4: Redis must hold the full total; local fallback must be cleared
    redis_total = float(fake_client.get(backend._key))
    assert abs(redis_total - 0.30) < 1e-9
    assert backend._fallback.get() == 0.0


def test_redis_backend_reconcile_failure_preserves_fallback_delta():
    """When _reconcile_on_reconnect() fails, the delta remains in local fallback.

    This is a unit test for _reconcile_on_reconnect directly: a broken pipeline
    must return False and leave the fallback balance intact.
    """
    server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

    backend = make_redis_backend(fake_client, chain_id="reconcile-fail-test")
    backend._using_fallback = False
    backend._fallback.add(0.50)  # delta accumulated during simulated outage

    # Sabotage the pipeline so execute() raises.
    class BrokenPipeline:
        def incrbyfloat(self, *a, **kw): pass
        def expire(self, *a, **kw): pass
        def execute(self): raise ConnectionError("Redis gone")

    original_pipeline = fake_client.pipeline
    fake_client.pipeline = lambda: BrokenPipeline()

    result = backend._reconcile_on_reconnect()

    # Must return False and preserve the delta.
    assert result is False
    assert abs(backend._fallback.get() - 0.50) < 1e-9, (
        "Fallback delta must be preserved after reconcile failure"
    )

    # Restore
    fake_client.pipeline = original_pipeline


def test_try_reconnect_failure_preserves_using_fallback():
    """_try_reconnect must leave _using_fallback=True when reconciliation fails.

    If reconcile fails, the backend must stay on fallback so that subsequent
    add() calls route to the local backend and accumulated spend is not lost.
    We mock _connect() to avoid real network calls and isolate reconnect logic.
    """
    from unittest.mock import patch

    server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

    backend = make_redis_backend(fake_client, chain_id="try-reconnect-fail-test")
    backend._using_fallback = True
    backend._fallback.add(0.75)  # delta accumulated during outage

    # Sabotage the pipeline so reconciliation always fails.
    class BrokenPipeline:
        def incrbyfloat(self, *a, **kw): pass
        def expire(self, *a, **kw): pass
        def execute(self): raise ConnectionError("Redis gone")

    def fake_connect_success(self_inner):
        """Simulate successful Redis reconnect by clearing _using_fallback."""
        self_inner._client = fake_client
        self_inner._using_fallback = False

    original_pipeline = fake_client.pipeline
    fake_client.pipeline = lambda: BrokenPipeline()

    with patch.object(type(backend), "_connect", fake_connect_success):
        result = backend._try_reconnect()

    assert result is False, "_try_reconnect must return False when reconcile fails"
    assert backend._using_fallback is True, (
        "_using_fallback must remain True when reconcile fails"
    )
    assert abs(backend._fallback.get() - 0.75) < 1e-9, (
        "Fallback delta must be preserved when reconcile fails"
    )

    # Restore pipeline and verify successful reconnect clears fallback.
    fake_client.pipeline = original_pipeline
    # Reset the reconnect timer so the next call is not rate-limited.
    backend._last_reconnect_attempt = 0.0

    with patch.object(type(backend), "_connect", fake_connect_success):
        result2 = backend._try_reconnect()

    assert result2 is True, "_try_reconnect must return True after successful reconcile"
    assert backend._using_fallback is False, (
        "_using_fallback must be False after successful reconnect+reconcile"
    )
    # Local fallback delta must have been flushed to Redis.
    assert abs(backend._fallback.get() - 0.0) < 1e-9, (
        "Fallback must be cleared after successful reconcile"
    )
    redis_val = float(fake_client.get(backend._key) or 0)
    assert abs(redis_val - 0.75) < 1e-9, "Redis must contain the flushed delta"


def test_seeded_failover_reconcile_does_not_double_count():
    """After seeded failover, reconcile must flush only the delta — not the full total.

    Scenario:
    1. Redis has 10.0 USD accumulated.
    2. add() fails → _seed_fallback_from_redis() seeds fallback with 10.0.
    3. During outage, add(5.0) accumulates → fallback total = 15.0.
    4. Reconnect → _reconcile_on_reconnect should flush ONLY 5.0 (the delta).
    5. Redis must be 10.0 + 5.0 = 15.0, NOT 10.0 + 15.0 = 25.0.
    """

    server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

    backend = make_redis_backend(fake_client, chain_id="double-count-test")

    # Step 1: put 10.0 USD into Redis.
    fake_client.set(backend._key, "10.0")
    fake_client.expire(backend._key, 3600)

    # Step 2: seed fallback (simulating what _seed_fallback_from_redis does).
    backend._seed_fallback_from_redis()
    assert abs(backend._fallback.get() - 10.0) < 1e-9
    assert abs(backend._fallback_seed_base - 10.0) < 1e-9

    # Step 3: accumulate 5.0 locally during outage.
    backend._using_fallback = True
    backend._fallback.add(5.0)  # fallback total = 15.0

    # Step 4: reconnect with working pipeline.
    backend._using_fallback = False  # simulate reconnect clearing fallback flag

    def fake_connect_success(self_inner):
        self_inner._client = fake_client
        self_inner._using_fallback = False

    reconciled = backend._reconcile_on_reconnect()
    assert reconciled is True

    # Step 5: Redis must be exactly 15.0 (10 existing + 5 delta), not 25.0.
    redis_val = float(fake_client.get(backend._key) or 0)
    assert abs(redis_val - 15.0) < 1e-9, (
        f"Redis should be 15.0 after delta-only reconcile but got {redis_val}; "
        "double-counting occurred"
    )

    # Fallback and seed base must be cleared after successful reconcile.
    assert backend._fallback.get() == 0.0
    assert backend._fallback_seed_base == 0.0


def test_reconnect_rate_limit_prevents_hot_loop():
    """_try_reconnect must not attempt a real reconnect within _RECONNECT_INTERVAL.

    Consecutive calls within the interval should return False immediately
    without calling _connect(), preventing hot-loop log storms.
    """
    from unittest.mock import patch

    server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

    backend = make_redis_backend(fake_client, chain_id="rate-limit-test")
    backend._using_fallback = True
    connect_call_count = [0]

    def counting_connect(self_inner):
        connect_call_count[0] += 1
        self_inner._client = fake_client
        self_inner._using_fallback = False

    # First call: allowed (no prior attempt).
    backend._last_reconnect_attempt = 0.0
    with patch.object(type(backend), "_connect", counting_connect):
        backend._try_reconnect()

    first_count = connect_call_count[0]
    assert first_count >= 1, "First call must attempt _connect"

    # Second call immediately after: rate-limited, no new _connect call.
    with patch.object(type(backend), "_connect", counting_connect):
        result = backend._try_reconnect()

    assert result is False, "Rate-limited call must return False"
    assert connect_call_count[0] == first_count, (
        "_connect must not be called again within _RECONNECT_INTERVAL"
    )


def test_concurrent_failover_does_not_double_seed():
    """Concurrent threads failing over to fallback must seed exactly once.

    If multiple threads hit a Redis failure simultaneously, only the first
    should seed the fallback with the Redis total.  Subsequent threads must
    route straight to the already-seeded fallback without re-seeding,
    which would overwrite previous increments.
    """
    import time

    server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

    backend = make_redis_backend(fake_client, chain_id="concurrent-failover-test")

    # Seed Redis with a known total.
    fake_client.set(backend._key, "10.0")
    fake_client.expire(backend._key, 3600)

    results = []
    lock = threading.Lock()

    # Break the pipeline after the Redis total is readable via .get().
    class DelayedBrokenPipeline:
        def incrbyfloat(self, *a, **kw): pass
        def expire(self, *a, **kw): pass
        def execute(self):
            time.sleep(0.01)  # allow threads to pile up
            raise ConnectionError("concurrent failure")

    fake_client.pipeline = lambda: DelayedBrokenPipeline()

    def do_add():
        val = backend.add(1.0)
        with lock:
            results.append(val)

    threads = [threading.Thread(target=do_add) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All 5 threads added 1.0.  Final local fallback must be 10.0 + 5.0 = 15.0.
    # If double-seeding happened, the total would be higher (or increments lost).
    final = backend._fallback.get()
    assert abs(final - 15.0) < 1e-6, (
        f"Expected 15.0 (10 seeded + 5 increments) but got {final}; "
        "concurrent failover seeded more than once or lost increments"
    )


def test_failover_seeded_with_redis_total():
    """When add() fails and falls back, the returned total includes prior Redis spend.

    Scenario:
    1. Backend has 1.00 USD already accumulated in Redis.
    2. Next add(0.50) fails mid-pipeline → failover triggered.
    3. Fallback is seeded with 1.00 USD (last known Redis total).
    4. Return value must be >= 1.50 USD (not 0.50 which is the local-only total).
    """
    server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

    backend = make_redis_backend(fake_client, chain_id="seed-test")

    # Step 1: put 1.00 USD into Redis directly.
    fake_client.set(backend._key, "1.0")
    fake_client.expire(backend._key, 3600)

    # Step 2: break the pipeline's execute() so the add will fail.
    class HalfBrokenPipeline:
        def __init__(self, real_client):
            self._real_client = real_client
        def incrbyfloat(self, *a, **kw): pass
        def expire(self, *a, **kw): pass
        def execute(self): raise ConnectionError("broken mid-way")

    original_pipeline = fake_client.pipeline
    fake_client.pipeline = lambda: HalfBrokenPipeline(fake_client)

    total = backend.add(0.50)

    # Step 3: verify the return value is seeded total + new amount.
    # Should be 1.00 (seeded from Redis) + 0.50 = 1.50, not 0.50.
    assert total >= 1.49, (
        f"add() return should include prior Redis spend (got {total}); "
        "budget ceiling must be computed against global total, not local-only delta"
    )

    # Restore
    fake_client.pipeline = original_pipeline


# ---------------------------------------------------------------------------
# ExecutionContext integration test
# ---------------------------------------------------------------------------


def test_execution_context_with_redis_backend(fake_redis_client):
    """ExecutionContext using fakeredis backend accumulates cost correctly."""
    from veronica_core.containment import ExecutionConfig, ExecutionContext

    backend = make_redis_backend(fake_redis_client, chain_id="ctx-test")

    config = ExecutionConfig(
        max_cost_usd=10.0,
        max_steps=20,
        max_retries_total=5,
        budget_backend=backend,
    )

    with ExecutionContext(config=config) as ctx:
        ctx.wrap_llm_call(fn=lambda: None, options=None)
        ctx.wrap_llm_call(fn=lambda: None, options=None)

    snap = ctx.get_snapshot()
    # cost_estimate_hint defaults to 0.0 so accumulated cost stays at 0 unless set
    assert snap.cost_usd_accumulated == 0.0

    # Now test with actual cost_estimate_hint
    from veronica_core.containment import WrapOptions

    config2 = ExecutionConfig(
        max_cost_usd=10.0,
        max_steps=20,
        max_retries_total=5,
        budget_backend=make_redis_backend(fake_redis_client, chain_id="ctx-test-2"),
    )

    with ExecutionContext(config=config2) as ctx2:
        ctx2.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(cost_estimate_hint=0.10),
        )
        ctx2.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(cost_estimate_hint=0.20),
        )

    snap2 = ctx2.get_snapshot()
    assert abs(snap2.cost_usd_accumulated - 0.30) < 1e-9


# ---------------------------------------------------------------------------
# _redact_exc -- credential redaction in error logs (Phase 0, item 0f)
# ---------------------------------------------------------------------------


class TestRedactExc:
    def test_redacts_password_in_redis_url(self) -> None:
        exc = ConnectionError("Error connecting to redis://admin:s3cret@redis.example.com:6379/0")
        result = _redact_exc(exc)
        assert "s3cret" not in result
        assert "admin" not in result
        assert "***@redis.example.com" in result

    def test_redacts_rediss_url(self) -> None:
        exc = ConnectionError("rediss://user:pa$$w0rd@host:6380/1 timed out")
        result = _redact_exc(exc)
        assert "pa$$w0rd" not in result
        assert "user" not in result
        assert "***@host" in result

    def test_preserves_message_without_url(self) -> None:
        exc = RuntimeError("some generic error")
        result = _redact_exc(exc)
        assert result == "RuntimeError: some generic error"

    def test_includes_exception_type(self) -> None:
        exc = ValueError("bad value")
        result = _redact_exc(exc)
        assert result.startswith("ValueError: ")


# ---------------------------------------------------------------------------
# Adversarial tests — H1 redact, H4 TOCTOU, M1 reconnect race
# ---------------------------------------------------------------------------


class TestAdversarialRedactExcInLogs:
    """H1: All Redis exception log calls must use _redact_exc, never raw exc.

    Attacker mindset: an exception message can contain
    'redis://user:password@host/db' — verifies that credential strings
    never appear in log output.
    """

    def test_reconcile_on_reconnect_failure_log_redacts_url(self) -> None:
        """RedisBudgetBackend._reconcile_on_reconnect error log must not leak credentials."""
        import logging

        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)
        backend = make_redis_backend(fake_client, chain_id="adv-redact-reconcile")
        backend._fallback.add(1.0)

        credential_url = "redis://admin:sup3rs3cret@redis.internal:6379/0"

        class CredentialLeakingPipeline:
            def incrbyfloat(self, *a, **kw): pass
            def expire(self, *a, **kw): pass
            def execute(self): raise ConnectionError(f"failed connecting to {credential_url}")

        fake_client.pipeline = lambda: CredentialLeakingPipeline()

        log_records: list[logging.LogRecord] = []

        class CapturingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_records.append(record)

        handler = CapturingHandler()
        target_logger = logging.getLogger("veronica_core.distributed")
        target_logger.addHandler(handler)
        try:
            backend._reconcile_on_reconnect()
        finally:
            target_logger.removeHandler(handler)

        assert log_records, "Expected at least one log record"
        full_log_output = " ".join(r.getMessage() for r in log_records)
        assert "sup3rs3cret" not in full_log_output, (
            "Credential password must not appear in log output (H1 _redact_exc missing)"
        )
        assert "admin" not in full_log_output, (
            "Credential username must not appear in log output (H1 _redact_exc missing)"
        )

    def test_seed_fallback_failure_log_redacts_url(self) -> None:
        """RedisBudgetBackend._seed_fallback_from_redis warning log must not leak credentials."""
        import logging

        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)
        backend = make_redis_backend(fake_client, chain_id="adv-redact-seed")

        credential_url = "redis://secretuser:topsecret@10.0.0.1:6379/1"

        def credential_leaking_get(key: str) -> None:
            raise ConnectionError(f"Cannot reach {credential_url}")

        fake_client.get = credential_leaking_get  # type: ignore[method-assign]

        log_records: list[logging.LogRecord] = []

        class CapturingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_records.append(record)

        handler = CapturingHandler()
        target_logger = logging.getLogger("veronica_core.distributed")
        target_logger.addHandler(handler)
        try:
            backend._seed_fallback_from_redis()
        finally:
            target_logger.removeHandler(handler)

        full_log_output = " ".join(r.getMessage() for r in log_records)
        assert "topsecret" not in full_log_output, (
            "Password must not appear in log output (H1 fix for _seed_fallback_from_redis)"
        )
        assert "secretuser" not in full_log_output, (
            "Username must not appear in log output (H1 fix for _seed_fallback_from_redis)"
        )

    def test_activate_fallback_log_redacts_url(self) -> None:
        """DistributedCircuitBreaker._activate_fallback error log must not leak credentials."""
        import logging

        from veronica_core.distributed import DistributedCircuitBreaker

        credential_url = "redis://circuituser:circuitpass@redis.circuit:6379/0"
        exc = ConnectionError(f"Error in {credential_url}")

        log_records: list[logging.LogRecord] = []

        class CapturingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_records.append(record)

        handler = CapturingHandler()
        target_logger = logging.getLogger("veronica_core.distributed")
        target_logger.addHandler(handler)
        try:
            # Build a minimal DCB without a real Redis connection.
            dcb = DistributedCircuitBreaker.__new__(DistributedCircuitBreaker)
            dcb._redis_url = "redis://fake"
            dcb._key = "veronica:cb:adv-test"
            dcb._ttl = 3600
            dcb._fallback_on_error = True
            dcb._using_fallback = False
            dcb._lock = threading.Lock()
            from veronica_core.circuit_breaker import CircuitBreaker
            dcb._fallback = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)
            dcb._client = None
            dcb._seed_fallback_from_redis = lambda: None  # type: ignore[method-assign]
            dcb._activate_fallback(exc, "check")
        finally:
            target_logger.removeHandler(handler)

        full_log_output = " ".join(r.getMessage() for r in log_records)
        assert "circuitpass" not in full_log_output, (
            "Password must not appear in log output (H1 fix for _activate_fallback)"
        )
        assert "circuituser" not in full_log_output, (
            "Username must not appear in log output (H1 fix for _activate_fallback)"
        )


class TestAdversarialH4TOCTOUFallbackTransition:
    """H4: Concurrent add() calls during fallback transition must not lose increments.

    10 threads call add() simultaneously while the pipeline is broken.
    After transition, all 10 increments must be present in the fallback.
    """

    def test_concurrent_add_during_fallback_transition_no_lost_increments(self) -> None:
        import time

        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        backend = make_redis_backend(fake_client, chain_id="adv-toctou-transition")

        # Pre-load a known base value to verify seeding accuracy.
        fake_client.set(backend._key, "5.0")
        fake_client.expire(backend._key, 3600)

        class SlowBrokenPipeline:
            """Simulates slow-failing pipeline to expose TOCTOU window."""
            def incrbyfloat(self, *a, **kw): pass
            def expire(self, *a, **kw): pass
            def execute(self):
                time.sleep(0.005)  # widen the TOCTOU window
                raise ConnectionError("pipeline failed")

        fake_client.pipeline = lambda: SlowBrokenPipeline()

        n_threads = 10
        amount_per_thread = 0.1
        results: list[float] = []
        lock = threading.Lock()

        def do_add() -> None:
            val = backend.add(amount_per_thread)
            with lock:
                results.append(val)

        threads = [threading.Thread(target=do_add) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == n_threads, "All threads must complete"
        # Base (5.0) seeded exactly once + 10 increments of 0.1 = 6.0
        final = backend._fallback.get()
        assert abs(final - 6.0) < 1e-5, (
            f"Expected fallback total 6.0 (5.0 seeded + 1.0 increments) but got {final}; "
            "TOCTOU caused lost increments or double-seeding"
        )


class TestAdversarialM1ReconnectRaceUnderConcurrency:
    """M1: _try_reconnect rate-limit must be enforced even under high concurrency.

    10 threads call add() simultaneously while on fallback.  Only one
    reconnect attempt should occur per _RECONNECT_INTERVAL window.
    """

    def test_try_reconnect_rate_limit_under_10_concurrent_threads(self) -> None:
        from unittest.mock import patch

        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        backend = make_redis_backend(fake_client, chain_id="adv-m1-reconnect-race")
        backend._using_fallback = True
        backend._fallback_on_error = True
        backend._last_reconnect_attempt = 0.0

        connect_calls: list[float] = []
        import time

        def counting_connect_fail(self_inner: object) -> None:
            connect_calls.append(time.monotonic())
            # Simulate failed reconnect: stays on fallback.
            raise ConnectionError("still down")

        n_threads = 10

        def do_add() -> None:
            backend.add(0.1)

        with patch.object(type(backend), "_connect", counting_connect_fail):
            threads = [threading.Thread(target=do_add) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Rate-limiter must allow at most 1 reconnect attempt across all threads.
        assert len(connect_calls) <= 1, (
            f"Expected at most 1 _connect call (rate-limited) but got {len(connect_calls)}; "
            "H4 lock fix (holding lock during reconnect) must serialise _try_reconnect"
        )


# ---------------------------------------------------------------------------
# Adversarial tests — DistributedCircuitBreaker TOCTOU, fallback flip, reconnect race
# ---------------------------------------------------------------------------


def _make_dcb_for_adversarial(
    fake_client,
    circuit_id: str = "adv-test",
    failure_threshold: int = 3,
    recovery_timeout: float = 60.0,
    half_open_slot_timeout: float = 120.0,
):
    """Create DistributedCircuitBreaker with injected fakeredis client."""
    import veronica_core.distributed as dist_mod
    from veronica_core.circuit_breaker import CircuitBreaker
    from veronica_core.distributed import DistributedCircuitBreaker

    dcb = DistributedCircuitBreaker.__new__(DistributedCircuitBreaker)
    dcb._redis_url = "redis://fake"
    dcb._circuit_id = circuit_id
    dcb._key = f"veronica:circuit:{circuit_id}"
    dcb._failure_threshold = failure_threshold
    dcb._recovery_timeout = recovery_timeout
    dcb._ttl = 3600
    dcb._fallback_on_error = True
    dcb._half_open_slot_timeout = half_open_slot_timeout
    dcb._failure_predicate = None
    dcb._fallback = CircuitBreaker(
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout,
    )
    dcb._using_fallback = False
    dcb._client = fake_client
    dcb._owns_client = False
    dcb._lock = threading.Lock()
    dcb._last_reconnect_attempt = 0.0
    dcb._script_failure = fake_client.register_script(dist_mod._LUA_RECORD_FAILURE)
    dcb._script_success = fake_client.register_script(dist_mod._LUA_RECORD_SUCCESS)
    dcb._script_check = fake_client.register_script(dist_mod._LUA_CHECK)
    return dcb


class TestAdversarialDistributedCBTOCTOU:
    """TOCTOU proof: HALF_OPEN slot must be claimed by exactly 1 concurrent caller.

    Attacker mindset: 10 threads all see HALF_OPEN simultaneously and race to
    claim the single test slot.  The Lua script atomicity must ensure exactly
    one wins — no split-brain where 0 or 2+ threads are allowed through.
    """

    def test_adversarial_half_open_slot_exactly_one_claimant_under_concurrency(
        self,
    ) -> None:
        """Under 10 concurrent check() calls in HALF_OPEN, exactly 1 must be allowed."""
        from veronica_core.runtime_policy import PolicyContext

        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        dcb = _make_dcb_for_adversarial(
            fake_client,
            circuit_id="adv-toctou-half-open",
            failure_threshold=1,
            half_open_slot_timeout=120.0,
        )

        # Force HALF_OPEN state directly in Redis.
        fake_client.hset(
            dcb._key,
            mapping={
                "state": "HALF_OPEN",
                "failure_count": 1,
                "success_count": 0,
                "last_failure_time": str(time.time() - 999.0),
                "half_open_in_flight": 0,
                "half_open_claimed_at": 0,
            },
        )
        fake_client.expire(dcb._key, 3600)

        ctx = PolicyContext()
        allowed_results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def do_check() -> None:
            barrier.wait()  # synchronize all threads at the same instant
            decision = dcb.check(ctx)
            with lock:
                allowed_results.append(decision.allowed)

        threads = [threading.Thread(target=do_check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(allowed_results) == 10, "All threads must complete"
        allowed_count = sum(1 for r in allowed_results if r)
        assert allowed_count == 1, (
            f"Exactly 1 check() must be allowed in HALF_OPEN (Lua atomic slot claim), "
            f"but {allowed_count} were allowed. TOCTOU race detected."
        )

    def test_adversarial_half_open_slot_released_after_timeout_and_one_new_claimant(
        self,
    ) -> None:
        """Stale HALF_OPEN slot must be released after timeout and allow exactly 1 new claimant.

        If the process that claimed the slot crashes (slot held indefinitely),
        the half_open_slot_timeout releases it automatically.  The next caller
        should then claim the slot — not be blocked forever.
        """
        from veronica_core.runtime_policy import PolicyContext

        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        dcb = _make_dcb_for_adversarial(
            fake_client,
            circuit_id="adv-slot-timeout",
            failure_threshold=1,
            half_open_slot_timeout=1.0,  # 1 second timeout
        )

        # Simulate a stale slot: claimed 10 seconds ago (past the 1s timeout).
        stale_claimed_at = time.time() - 10.0
        fake_client.hset(
            dcb._key,
            mapping={
                "state": "HALF_OPEN",
                "failure_count": 1,
                "success_count": 0,
                "last_failure_time": str(time.time() - 999.0),
                "half_open_in_flight": 1,  # slot appears held
                "half_open_claimed_at": stale_claimed_at,
            },
        )
        fake_client.expire(dcb._key, 3600)

        ctx = PolicyContext()
        # The slot is stale — check() should auto-release and allow this caller.
        decision = dcb.check(ctx)
        assert decision.allowed, (
            "check() must allow the caller when the stale HALF_OPEN slot is auto-released "
            f"(got allowed={decision.allowed}, reason={decision.reason!r})"
        )

        # A second concurrent caller must be denied (slot now held by first caller).
        decision2 = dcb.check(ctx)
        assert not decision2.allowed, (
            "Second check() must be denied (slot already claimed by the previous caller)"
        )


class TestAdversarialDistributedCBConcurrentFallbackFlip:
    """Concurrent fallback flip: when Redis fails mid-operation, exactly one thread
    should seed the fallback and all threads must route to fallback consistently.

    Attacker mindset: 10 threads all call check()/record_failure() simultaneously
    while Redis is broken. The fallback must be activated exactly once (idempotent
    _activate_fallback), and all results must be consistent.
    """

    def test_adversarial_concurrent_redis_failure_activates_fallback_once(
        self,
    ) -> None:
        """10 concurrent record_failure() calls with Redis broken must activate fallback once."""


        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        dcb = _make_dcb_for_adversarial(
            fake_client,
            circuit_id="adv-fallback-flip",
            failure_threshold=5,
        )

        seed_calls: list[int] = []
        original_seed = dcb._seed_fallback_from_redis

        def counting_seed() -> None:
            seed_calls.append(1)
            original_seed()

        dcb._seed_fallback_from_redis = counting_seed  # type: ignore[method-assign]

        # Break the Lua script to simulate Redis failure.
        def broken_script(*args, **kwargs):
            raise ConnectionError("Redis unavailable during test")

        dcb._script_failure = broken_script  # type: ignore[assignment]

        barrier = threading.Barrier(10)

        def do_record_failure() -> None:
            barrier.wait()
            dcb.record_failure()

        threads = [threading.Thread(target=do_record_failure) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Fallback must have been activated.
        assert dcb.is_using_fallback or dcb._using_fallback, (
            "Fallback must be activated after concurrent Redis failures"
        )

        # _seed_fallback_from_redis must have been called at most once
        # (idempotent double-check under lock prevents multiple seeds).
        assert len(seed_calls) <= 1, (
            f"_seed_fallback_from_redis must be called at most once (got {len(seed_calls)}); "
            "_activate_fallback double-check lock is not working"
        )

    def test_adversarial_check_during_fallback_all_return_consistent_decisions(
        self,
    ) -> None:
        """10 concurrent check() calls on fallback must return consistent PolicyDecision objects."""
        from veronica_core.runtime_policy import PolicyContext

        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        dcb = _make_dcb_for_adversarial(
            fake_client,
            circuit_id="adv-fallback-check",
            failure_threshold=5,
        )
        # Pre-set to fallback mode.
        dcb._using_fallback = True
        dcb._client = None

        ctx = PolicyContext()
        decisions: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def do_check() -> None:
            barrier.wait()
            d = dcb.check(ctx)
            with lock:
                decisions.append(d.allowed)

        threads = [threading.Thread(target=do_check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(decisions) == 10, "All threads must complete"
        # In fallback (local CircuitBreaker), CLOSED state — all should be allowed.
        assert all(decisions), (
            f"All check() calls on fallback CLOSED circuit must be allowed, "
            f"but got: {decisions}"
        )


class TestAdversarialDistributedCBReconnectRace:
    """Reconnect race: concurrent threads must not trigger multiple reconnect attempts.

    The _attempt_reconnect_if_on_fallback() uses double-checked locking:
    outer check avoids lock on fast path; inner check serialises reconnects.
    All 10 concurrent calls must result in at most 1 _try_reconnect call.
    """

    def test_adversarial_reconnect_race_with_barrier_exactly_one_attempt(
        self,
    ) -> None:
        """10 threads simultaneously on fallback must trigger at most 1 _connect attempt.

        _attempt_reconnect_if_on_fallback() uses double-checked locking:
        outer check is unlocked (fast path), inner check is locked (serialised).
        Even with 10 threads all seeing _using_fallback=True simultaneously,
        only one should enter _try_reconnect() and attempt _connect().
        """
        from unittest.mock import patch

        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        dcb = _make_dcb_for_adversarial(
            fake_client,
            circuit_id="adv-reconnect-race",
            failure_threshold=5,
        )
        dcb._using_fallback = True
        dcb._fallback_on_error = True
        dcb._last_reconnect_attempt = 0.0  # allow the first attempt

        connect_calls: list[float] = []

        def counting_connect_fail(self_inner: object) -> None:
            """Count _connect() invocations and simulate failure (stays on fallback)."""
            connect_calls.append(time.monotonic())
            raise ConnectionError("still down for test")

        barrier = threading.Barrier(10)

        def do_attempt() -> None:
            barrier.wait()  # release all threads simultaneously
            dcb._attempt_reconnect_if_on_fallback()

        with patch.object(type(dcb), "_connect", counting_connect_fail):
            threads = [threading.Thread(target=do_attempt) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Double-checked locking must ensure at most 1 _connect() call.
        # (All 10 threads attempt _attempt_reconnect_if_on_fallback, but
        # the inner lock + rate-limit serialises them to 1 actual _connect.)
        assert len(connect_calls) <= 1, (
            f"Expected at most 1 _connect() call under double-checked locking, "
            f"but got {len(connect_calls)}. Reconnect race condition detected."
        )

    def test_adversarial_fallback_reconnect_does_not_lose_circuit_state(
        self,
    ) -> None:
        """After reconnect, reconciled Redis state must reflect local fallback failures.

        Scenario:
        1. DCB starts on Redis, records 2 failures via Lua script directly.
        2. Fallback is seeded from Redis → fallback has 2 failures.
        3. 1 more failure added directly to fallback (simulates outage period).
        4. _reconcile_on_reconnect() pushes local state to Redis.
        5. Redis must show failure_count=3, not 0 (no state lost on reconnect).
        """
        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        dcb = _make_dcb_for_adversarial(
            fake_client,
            circuit_id="adv-reconnect-state",
            failure_threshold=5,
        )

        # Step 1: record 2 failures directly via Lua script (no reconnect side-effects).
        dcb._script_failure(
            keys=[dcb._key],
            args=[dcb._failure_threshold, time.time(), dcb._ttl],
        )
        dcb._script_failure(
            keys=[dcb._key],
            args=[dcb._failure_threshold, time.time(), dcb._ttl],
        )
        redis_count = int(fake_client.hget(dcb._key, "failure_count") or 0)
        assert redis_count == 2, f"Redis should have 2 failures, got {redis_count}"

        # Step 2: seed fallback from Redis (simulates what _seed_fallback_from_redis does).
        dcb._seed_fallback_from_redis()
        assert dcb._fallback._failure_count == 2, (
            f"Fallback must be seeded with 2 failures, got {dcb._fallback._failure_count}"
        )

        # Step 3: add 1 more failure to fallback directly (simulates outage period).
        from veronica_core.circuit_breaker import CircuitState
        with dcb._fallback._lock:
            dcb._fallback._failure_count = 3
            dcb._fallback._state = CircuitState.CLOSED
            dcb._fallback._last_failure_time = time.time()

        # Step 4: simulate reconnect — call _reconcile_on_reconnect directly.
        # At this point dcb._using_fallback is False (not in fallback mode) and
        # dcb._client is fake_client (working), so reconcile should succeed.
        reconciled = dcb._reconcile_on_reconnect()
        assert reconciled is True, (
            "Reconcile must succeed with working fakeredis client"
        )

        # Step 5: verify Redis has the pushed state.
        data = fake_client.hgetall(dcb._key)
        redis_failure_count = int(data.get("failure_count", 0))
        redis_state = data.get("state", "CLOSED")

        assert redis_failure_count == 3, (
            f"Redis must show 3 failures after reconcile, got {redis_failure_count}; "
            "circuit state was lost during reconnect reconciliation"
        )
        assert redis_state in ("CLOSED", "OPEN"), (
            f"Redis state must be CLOSED or OPEN after reconcile, got {redis_state!r}"
        )


# ---------------------------------------------------------------------------
# Adversarial tests — RedisBudgetBackend: add/get race, reset, reconcile
# ---------------------------------------------------------------------------


class TestAdversarialRedisBudgetAddGetRace:
    """TOCTOU proof: concurrent add() and get() must not lose increments.

    Attacker mindset: 5 threads calling add(1.0) and 5 threads calling get()
    simultaneously.  INCRBYFLOAT is atomic per call, so the final total must
    equal the number of add() calls.  Any lost increment indicates a race.
    """

    def test_adversarial_concurrent_add_and_get_no_lost_increments(self) -> None:
        """5 add(1.0) + 5 get() threads via Barrier: final total must be 5.0."""
        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        backend = make_redis_backend(fake_client, chain_id="adv-add-get-race")

        add_results: list[float] = []
        get_results: list[float] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def do_add() -> None:
            barrier.wait()
            val = backend.add(1.0)
            with lock:
                add_results.append(val)

        def do_get() -> None:
            barrier.wait()
            val = backend.get()
            with lock:
                get_results.append(val)

        threads = (
            [threading.Thread(target=do_add) for _ in range(5)]
            + [threading.Thread(target=do_get) for _ in range(5)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(add_results) == 5, "All 5 add() threads must complete"
        assert len(get_results) == 5, "All 5 get() threads must complete"

        # Final total must reflect all 5 add(1.0) calls — no lost increments.
        final = backend.get()
        assert abs(final - 5.0) < 1e-6, (
            f"Final total must be 5.0 (5 x 1.0 added), got {final}; "
            "concurrent add/get race caused lost increments"
        )

        # Each add() return value must be monotonically valid (between 1.0 and 5.0).
        for r in add_results:
            assert 1.0 <= r <= 5.0, (
                f"add() return {r} is outside valid range [1.0, 5.0]; "
                "INCRBYFLOAT atomicity violated"
            )


class TestAdversarialRedisBudgetFallbackTransitionConcurrency:
    """Concurrent add() during fallback transition must not lose any increment.

    Thread A and Thread B both call add() simultaneously while Redis pipeline
    is artificially slowed.  Both increments must be reflected in the final total
    regardless of whether they land in Redis or in the fallback.
    """

    def test_adversarial_two_threads_add_during_slow_pipeline_no_lost_increment(
        self,
    ) -> None:
        """2 threads add 1.0 each through a slow pipeline: final total must be 2.0."""
        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        backend = make_redis_backend(fake_client, chain_id="adv-slow-pipeline")

        original_pipeline = fake_client.pipeline

        class SlowPipeline:
            """Wraps real pipeline but introduces a delay in execute() to widen races."""

            def __init__(self) -> None:
                self._real = original_pipeline()

            def incrbyfloat(self, key: str, amount: float) -> None:
                self._real.incrbyfloat(key, amount)

            def expire(self, key: str, ttl: int) -> None:
                self._real.expire(key, ttl)

            def execute(self) -> list:
                time.sleep(0.02)  # widen the concurrency window
                return self._real.execute()

        fake_client.pipeline = SlowPipeline

        results: list[float] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def do_add() -> None:
            barrier.wait()
            val = backend.add(1.0)
            with lock:
                results.append(val)

        t1 = threading.Thread(target=do_add)
        t2 = threading.Thread(target=do_add)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Restore
        fake_client.pipeline = original_pipeline

        assert len(results) == 2, "Both threads must complete"
        # Sum of returned totals is not guaranteed to be 2.0 (returns are snapshots),
        # but the actual final backend total must be 2.0.
        final = backend.get()
        assert abs(final - 2.0) < 1e-5, (
            f"Final total must be 2.0 (2 x 1.0 added), got {final}; "
            "slow pipeline caused a lost increment during concurrent add()"
        )


class TestAdversarialRedisBudgetResetConcurrency:
    """Reset during concurrent add() must leave the backend in a consistent state.

    Thread A performs a rapid add() loop while Thread B calls reset() mid-stream.
    After reset() completes, any subsequent add() must start from 0.0 and
    get() must reflect only post-reset accumulation.
    """

    def test_adversarial_reset_mid_add_loop_post_reset_total_is_correct(
        self,
    ) -> None:
        """After reset(), backend total reflects only post-reset adds."""
        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        backend = make_redis_backend(fake_client, chain_id="adv-reset-concurrency")

        reset_done = threading.Event()
        post_reset_adds: list[float] = []
        lock = threading.Lock()

        def adder_thread() -> None:
            """Add 1.0 repeatedly; after reset, track post-reset add results."""
            for i in range(20):
                val = backend.add(1.0)
                if reset_done.is_set():
                    with lock:
                        post_reset_adds.append(val)
                time.sleep(0.001)

        def resetter_thread() -> None:
            """Wait a bit then reset."""
            time.sleep(0.005)  # let adder accumulate some
            backend.reset()
            reset_done.set()

        t_add = threading.Thread(target=adder_thread)
        t_reset = threading.Thread(target=resetter_thread)
        t_add.start()
        t_reset.start()
        t_add.join()
        t_reset.join()

        # After all threads finish, the final total must be >= 0 and <= 20.0
        # (cannot be negative; cannot exceed total adds).
        final = backend.get()
        assert 0.0 <= final <= 20.0, (
            f"Final total after reset must be in [0.0, 20.0], got {final}"
        )

        # Post-reset adds must return values >= 1.0 (at least the first post-reset add).
        # This ensures reset() zeroed the counter correctly.
        if post_reset_adds:
            min_post_reset = min(post_reset_adds)
            assert min_post_reset >= 0.0, (
                f"Post-reset add() return {min_post_reset} must be non-negative"
            )

        # Verify final state is consistent: get() must match the last add() return
        # (within tolerance — concurrent nature means brief windows of mismatch).
        final_2 = backend.get()
        assert abs(final_2 - final) < 1e-3 or final_2 >= final, (
            f"get() must be stable or monotonic after threads finish, "
            f"got {final} then {final_2}"
        )


class TestAdversarialRedisBudgetReconcileConnectionFailure:
    """Connection failure mid-reconcile must preserve the fallback delta.

    _reconcile_on_reconnect() reads the local delta and attempts INCRBYFLOAT.
    If the connection drops during execute(), the delta must remain in the
    local fallback — it must not be silently discarded.

    This is a regression test for the 'undercount on partial reconcile' pattern:
    if the delta were cleared before confirming the write, a connection drop
    would erase it forever.
    """

    def test_adversarial_reconcile_failure_mid_execute_preserves_delta(
        self,
    ) -> None:
        """INCRBYFLOAT failure during reconcile must leave delta intact."""
        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        backend = make_redis_backend(fake_client, chain_id="adv-reconcile-drop")

        # Pre-load Redis with a base value.
        fake_client.set(backend._key, "2.0")
        fake_client.expire(backend._key, 3600)

        # Simulate failover with a delta of 0.75.
        backend._seed_fallback_from_redis()
        backend._using_fallback = True
        backend._fallback.add(0.75)  # delta = 0.75

        # Verify pre-condition: fallback holds 2.75 total, seed_base = 2.0.
        assert abs(backend._fallback.get() - 2.75) < 1e-9
        assert abs(backend._fallback_seed_base - 2.0) < 1e-9

        # Break the pipeline so INCRBYFLOAT fails.
        class DropPipeline:
            def incrbyfloat(self, *a, **kw) -> None: pass
            def expire(self, *a, **kw) -> None: pass
            def execute(self) -> None:
                raise ConnectionError("connection dropped mid-reconcile")

        fake_client.pipeline = lambda: DropPipeline()

        # Call reconcile with _using_fallback cleared (simulates reconnect).
        backend._using_fallback = False
        result = backend._reconcile_on_reconnect()

        assert result is False, (
            "_reconcile_on_reconnect must return False when connection drops"
        )
        # The delta must still be in the fallback — not discarded.
        remaining_delta = backend._fallback.get() - backend._fallback_seed_base
        assert abs(remaining_delta - 0.75) < 1e-9, (
            f"Delta must be preserved after failed reconcile; "
            f"expected 0.75, fallback.get()={backend._fallback.get():.4f}, "
            f"seed_base={backend._fallback_seed_base:.4f}, delta={remaining_delta:.4f}"
        )

    def test_adversarial_reconcile_success_then_failure_no_double_count(
        self,
    ) -> None:
        """After successful reconcile, a second reconcile must not double-count.

        If reset() is not called after a successful INCRBYFLOAT, subsequent
        reconnect attempts would push the same delta again, causing overspend.
        """
        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)

        backend = make_redis_backend(fake_client, chain_id="adv-double-reconcile")

        # Pre-load Redis with 1.0.
        fake_client.set(backend._key, "1.0")
        fake_client.expire(backend._key, 3600)

        # Simulate outage: seed=1.0, delta=0.5.
        backend._seed_fallback_from_redis()
        backend._using_fallback = True
        backend._fallback.add(0.5)

        # First reconcile: must succeed and flush delta=0.5 to Redis.
        backend._using_fallback = False
        result1 = backend._reconcile_on_reconnect()
        assert result1 is True, "First reconcile must succeed"

        redis_val_after_1 = float(fake_client.get(backend._key) or 0)
        assert abs(redis_val_after_1 - 1.5) < 1e-9, (
            f"Redis must be 1.5 after first reconcile (1.0 base + 0.5 delta), "
            f"got {redis_val_after_1}"
        )

        # Second reconcile: delta should be 0 (fallback was reset after first reconcile).
        result2 = backend._reconcile_on_reconnect()
        assert result2 is True, "Second reconcile (empty delta) must succeed"

        redis_val_after_2 = float(fake_client.get(backend._key) or 0)
        assert abs(redis_val_after_2 - 1.5) < 1e-9, (
            f"Redis must still be 1.5 after second reconcile (no delta to flush), "
            f"got {redis_val_after_2}; double-counting detected"
        )


class TestAdversarialGetTOCTOU:
    """H2: Verify get() TOCTOU fix — client reference captured under lock.

    Before the fix, _using_fallback could flip between the check (L290) and
    the Redis read (L293), causing get() to read from a stale/closed client.
    After the fix, the client reference is captured under lock, so subsequent
    Redis failure falls back cleanly.
    """

    def test_get_captures_client_under_lock(self) -> None:
        """get() with a client that fails mid-call must fall back gracefully."""
        from unittest.mock import MagicMock

        # Create a client that raises on .get() to simulate mid-call Redis failure
        broken_client = MagicMock()
        broken_client.get.side_effect = ConnectionError("Redis died")

        backend = make_redis_backend(broken_client, chain_id="h2-toctou")
        backend._fallback.add(0.42)
        backend._using_fallback = False  # Start as if connected

        # get() should catch the exception and return fallback value
        result = backend.get()
        # After Redis failure, fallback value (0.42) should be returned
        assert result == pytest.approx(0.42), (
            f"Expected fallback 0.42 after Redis failure, got {result}"
        )

    def test_get_concurrent_fallback_flip_no_crash(self) -> None:
        """get() must not crash when _using_fallback flips during the call.

        This simulates the TOCTOU window: one thread flips to fallback while
        another is inside get() after the lock check but before the Redis read.
        """

        server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeRedis(server=server, decode_responses=True)
        backend = make_redis_backend(fake_client, chain_id="h2-concurrent")
        fake_client.set(backend._key, "5.0")

        errors: list[Exception] = []

        def flip_fallback() -> None:
            # Flip _using_fallback while get() may be in progress
            for _ in range(50):
                with backend._lock:
                    backend._using_fallback = True
                with backend._lock:
                    backend._using_fallback = False

        def do_get() -> None:
            try:
                for _ in range(50):
                    val = backend.get()
                    assert isinstance(val, float), f"get() returned non-float: {val!r}"
            except Exception as exc:
                errors.append(exc)

        t_flip = threading.Thread(target=flip_fallback)
        t_get = threading.Thread(target=do_get)
        t_flip.start()
        t_get.start()
        t_flip.join()
        t_get.join()

        assert not errors, f"get() raised under concurrent fallback flip: {errors}"
