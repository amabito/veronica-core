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
