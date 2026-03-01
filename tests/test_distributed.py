"""Tests for distributed budget backends (P1-2)."""
from __future__ import annotations

import threading

import fakeredis
import pytest

from veronica_core.distributed import (
    LocalBudgetBackend,
    RedisBudgetBackend,
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
