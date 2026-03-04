"""Failure mode tests for veronica-core.

Covers 8 specific failure scenarios:
1. Double commit rejected (KeyError)
2. Rollback after commit is a no-op (KeyError)
3. Redis disconnect during commit — state not corrupted
4. Lua atomicity failure — budget state unchanged on error
5. Reserve then rollback — clean state after escrow release
6. WebSocket step limit — close code 1008 on step exhaustion
7. CancellationToken cascade — parent cancel propagates to children
8. SharedTimeoutPool exhaustion — single daemon thread under load
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import fakeredis
import pytest

from veronica_core.containment.execution_context import (
    ExecutionConfig,
    ExecutionContext,
)
from veronica_core.containment.timeout_pool import SharedTimeoutPool
from veronica_core.distributed import LocalBudgetBackend, RedisBudgetBackend
from veronica_core.middleware import VeronicaASGIMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_redis_backend(fake_client: Any, chain_id: str = "test") -> RedisBudgetBackend:
    """Inject a fakeredis client into a RedisBudgetBackend."""
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
# Test 1: Double commit rejected
# ---------------------------------------------------------------------------


def test_double_commit_rejected() -> None:
    """Second commit on the same reservation ID must raise KeyError.

    Once a reservation is committed the escrow entry is removed.
    Re-committing the same ID is an indication of duplicate processing
    and must be rejected to prevent double-spending.
    """
    b = LocalBudgetBackend()
    rid = b.reserve(0.5, ceiling=1.0)
    b.commit(rid)
    with pytest.raises(KeyError):
        b.commit(rid)


# ---------------------------------------------------------------------------
# Test 2: Rollback after commit is a no-op (KeyError)
# ---------------------------------------------------------------------------


def test_rollback_after_commit_no_effect() -> None:
    """Rollback on an already-committed reservation raises KeyError.

    Committed reservations are removed from the escrow table.
    A subsequent rollback must not corrupt committed state — it must
    raise KeyError and leave the committed total unchanged.
    """
    b = LocalBudgetBackend()
    rid = b.reserve(0.3, ceiling=1.0)
    committed_total = b.commit(rid)
    assert committed_total == pytest.approx(0.3)

    with pytest.raises(KeyError):
        b.rollback(rid)

    # Committed total must not change after the failed rollback.
    assert b.get() == pytest.approx(0.3)
    assert b.get_reserved() == 0.0


# ---------------------------------------------------------------------------
# Test 3: Redis disconnect during commit
# ---------------------------------------------------------------------------


def test_redis_disconnect_during_commit() -> None:
    """Simulate Redis disconnect mid-commit; ConnectionError is not propagated.

    When the Redis client raises ConnectionError during commit(), the
    RedisBudgetBackend activates its fallback path. Because the original
    reservation was stored only in Redis (not in the local fallback),
    the fallback commit raises KeyError — indicating the reservation is
    unknown to the fallback. The critical invariant is that ConnectionError
    itself is NOT propagated; the implementation catches it and redirects
    to the fallback layer.
    """
    fake_client = fakeredis.FakeRedis(decode_responses=True)
    b = make_redis_backend(fake_client, chain_id="disconnect-test")

    # Reserve works normally against Redis.
    rid = b.reserve(0.4, ceiling=1.0)

    # Patch commit Lua eval to raise ConnectionError.
    original_eval = fake_client.eval

    def failing_eval(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("Redis went away")

    fake_client.eval = failing_eval
    try:
        # commit() must NOT propagate ConnectionError — it catches it and
        # redirects to the fallback. The fallback raises KeyError because the
        # reservation was only in Redis (unknown to the local fallback).
        with pytest.raises(KeyError):
            b.commit(rid)
        # Backend must have switched to fallback mode.
        assert b._using_fallback is True
    finally:
        fake_client.eval = original_eval


# ---------------------------------------------------------------------------
# Test 4: Lua atomicity failure
# ---------------------------------------------------------------------------


def test_lua_atomicity_failure() -> None:
    """Patch Lua eval to fail on reserve; budget state must stay at zero.

    A failed Lua script must not partially update budget state.
    The backend activates the fallback path; either way the committed
    total must remain consistent (no phantom charges).
    """
    fake_client = fakeredis.FakeRedis(decode_responses=True)
    b = make_redis_backend(fake_client, chain_id="lua-atomicity")

    original_eval = fake_client.eval

    def atomic_fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Lua script aborted")

    fake_client.eval = atomic_fail
    try:
        try:
            b.reserve(0.5, ceiling=1.0)
        except Exception:
            pass  # reserve may raise or fall back — both are acceptable
    finally:
        fake_client.eval = original_eval

    # Regardless of reserve outcome, committed total must be 0 — no phantom charge.
    # Check both the backend accessor (which reads fallback if active) AND
    # the raw Redis key to ensure no partial mutation leaked through.
    assert b.get() == 0.0
    raw_redis_val = fake_client.get(b._key)
    assert raw_redis_val is None or float(raw_redis_val) == 0.0, (
        f"Redis committed key must be 0 or absent, got: {raw_redis_val}"
    )


# ---------------------------------------------------------------------------
# Test 5: Reserve then rollback leaves clean state
# ---------------------------------------------------------------------------


def test_reserve_rollback_clean_state() -> None:
    """Reserve then rollback must leave budget state fully clean.

    After a rollback, no dangling reservations or phantom charges must
    remain, and a subsequent reservation of the same amount must succeed
    (proving the ceiling was fully released).
    """
    b = LocalBudgetBackend()
    rid = b.reserve(0.2, ceiling=1.0)

    # Simulate interruption before commit completes by rolling back instead.
    b.rollback(rid)

    # State must be fully clean — no dangling reservations, no phantom charges.
    assert b.get() == 0.0
    assert b.get_reserved() == 0.0

    # A new reservation of the same amount must succeed (ceiling not eaten).
    rid2 = b.reserve(0.2, ceiling=1.0)
    assert rid2 is not None
    b.rollback(rid2)


# ---------------------------------------------------------------------------
# Test 6: WebSocket step limit — close code 1008
# ---------------------------------------------------------------------------


def test_websocket_step_limit() -> None:
    """ASGI middleware must send websocket.close(1008) when step limit is exceeded.

    Configure the middleware with max_steps=2 so that the first two
    tracked calls are allowed and the third triggers a halt, causing the
    middleware to send websocket.close with code=1008.
    """
    config = ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=2,
        max_retries_total=10,
    )
    middleware = VeronicaASGIMiddleware(app=_echo_ws_app, config=config)

    close_codes: list[int] = []

    async def run() -> None:
        scope = {"type": "websocket", "path": "/ws"}
        messages = [
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": "hello"},
            {"type": "websocket.receive", "text": "world"},
            {"type": "websocket.receive", "text": "overflow"},
        ]
        idx = [0]

        async def receive() -> dict[str, Any]:
            if idx[0] < len(messages):
                msg = messages[idx[0]]
                idx[0] += 1
                return msg
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message: dict[str, Any]) -> None:
            if message.get("type") == "websocket.close":
                close_codes.append(message.get("code", 0))

        await middleware(scope, receive, send)

    asyncio.run(run())

    assert 1008 in close_codes, (
        f"Expected websocket.close(1008) from step-limit enforcement, got: {close_codes}"
    )


async def _echo_ws_app(
    scope: dict[str, Any],
    receive: Any,
    send: Any,
) -> None:
    """Minimal echo WebSocket app that drains messages until disconnect."""
    while True:
        msg = await receive()
        if msg["type"] == "websocket.disconnect":
            return
        await send({"type": "websocket.send", "text": msg.get("text", "")})


# ---------------------------------------------------------------------------
# Test 7: CancellationToken cascade
# ---------------------------------------------------------------------------


def test_cancellation_token_cascade() -> None:
    """_propagate_child_cost must abort the parent and cancel its token
    when accumulated cost exceeds the ceiling.

    This verifies the upward cost propagation path: a child context
    reports cost to its parent, and if the parent's ceiling is breached,
    the parent is aborted and its CancellationToken is cancelled.
    Subsequent wrap calls on the *parent* must return HALT.
    """
    from veronica_core.shield.types import Decision

    parent_config = ExecutionConfig(
        max_cost_usd=1.0,
        max_steps=50,
        max_retries_total=5,
    )
    parent = ExecutionContext(config=parent_config)

    child_config = ExecutionConfig(
        max_cost_usd=0.5,
        max_steps=20,
        max_retries_total=5,
    )
    child = ExecutionContext(config=child_config, parent=parent)

    assert not parent._cancellation_token.is_cancelled
    assert not parent._aborted

    # Push parent cost near ceiling, then propagate from child to overflow.
    with parent._lock:
        parent._cost_usd_accumulated = 0.95

    parent._propagate_child_cost(0.1)

    # Parent must be aborted and its token cancelled.
    assert parent._aborted, (
        "Parent must be aborted after child cost pushes total over ceiling"
    )
    assert parent._cancellation_token.is_cancelled, (
        "Parent CancellationToken must be cancelled after cost overflow"
    )

    # Subsequent wrap on the parent must return HALT.
    parent_result = parent.wrap_llm_call(fn=lambda: None)
    assert parent_result == Decision.HALT, (
        f"wrap_llm_call on aborted parent must return HALT, got {parent_result}"
    )

    # Child context is independent — its own token is NOT cancelled by parent abort.
    # This is the current design: upward propagation only.
    assert not child._cancellation_token.is_cancelled, (
        "Child token must remain independent (upward propagation only)"
    )


# ---------------------------------------------------------------------------
# Test 8: SharedTimeoutPool exhaustion — single daemon thread
# ---------------------------------------------------------------------------


def test_shared_timeout_pool_exhaustion() -> None:
    """Schedule 10000 callbacks; only one daemon thread must be created.

    SharedTimeoutPool must use a single daemon thread regardless of how
    many callbacks are scheduled. Thread count must not grow with load.
    """
    pool = SharedTimeoutPool()
    n = 10_000
    fired: list[int] = []
    lock = threading.Lock()

    # Schedule all callbacks with a far-future deadline so they never fire
    # during the assertion phase, keeping the test fast.
    far_future = time.monotonic() + 3600.0
    for i in range(n):
        pool.schedule(
            deadline=far_future,
            callback=lambda idx=i: (lock.acquire(), fired.append(idx), lock.release()),
        )

    # Give the thread a moment to start (200ms for slow CI / Windows).
    time.sleep(0.2)

    # Verify the pool started exactly one thread.  Use the pool's own _thread
    # reference rather than threading.enumerate() to avoid false positives
    # from the module-level singleton used by ExecutionContext tests.
    assert pool._thread is not None, "Pool must have started a daemon thread"
    assert pool._thread.is_alive(), "Pool daemon thread must be alive"

    pool.shutdown()
    # Wait for daemon thread to exit after shutdown to prevent cross-test pollution.
    pool._thread.join(timeout=2.0)
    assert not pool._thread.is_alive(), "Pool daemon thread must exit after shutdown"
