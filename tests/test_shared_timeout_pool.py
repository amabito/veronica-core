"""Tests for SharedTimeoutPool (Item 2f).

Covers:
1. Basic scheduling and callback firing
2. Cancellation before deadline
3. Multiple concurrent deadlines (priority ordering)
4. Fallback path when pool raises (conceptual -- tested via shutdown)
5. Adversarial: double-cancel is a no-op
6. Adversarial: callback exception does not crash the pool
7. Adversarial: schedule after shutdown raises
8. Adversarial: 50 concurrent schedules, no deadlock
9. ExecutionContext uses pool (no legacy thread spawned for normal case)
10. Pool cancel on context exit prevents callback from firing
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import wait_for

from veronica_core.containment.timeout_pool import SharedTimeoutPool
from veronica_core.containment.execution_context import (
    ExecutionConfig,
    ExecutionContext,
)
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pool() -> SharedTimeoutPool:
    """Return a fresh pool for each test (avoids cross-test state)."""
    return SharedTimeoutPool()


# ---------------------------------------------------------------------------
# Basic functionality
# ---------------------------------------------------------------------------


def test_callback_fires_after_deadline() -> None:
    """Callback is invoked at or after the scheduled deadline."""
    pool = _pool()
    fired: list[float] = []
    deadline = time.monotonic() + 0.1

    pool.schedule(deadline=deadline, callback=lambda: fired.append(time.monotonic()))

    wait_for(lambda: bool(fired), timeout=2.0, msg="Callback was never fired")

    assert fired[0] >= deadline - 0.005, (
        f"Callback fired too early: {fired[0]:.4f} < deadline {deadline:.4f}"
    )
    pool.shutdown()


def test_cancelled_callback_does_not_fire() -> None:
    """Cancelled handle causes callback to be skipped."""
    pool = _pool()
    fired: list[int] = []
    deadline = time.monotonic() + 0.15

    handle = pool.schedule(deadline=deadline, callback=lambda: fired.append(1))
    pool.cancel(handle)

    time.sleep(0.3)
    assert fired == [], f"Cancelled callback must not fire, got {fired}"
    pool.shutdown()


def test_multiple_callbacks_fire_in_deadline_order() -> None:
    """Multiple callbacks fire in deadline order."""
    pool = _pool()
    order: list[int] = []
    now = time.monotonic()

    pool.schedule(deadline=now + 0.2, callback=lambda: order.append(2))
    pool.schedule(deadline=now + 0.1, callback=lambda: order.append(1))
    pool.schedule(deadline=now + 0.3, callback=lambda: order.append(3))

    wait_for(
        lambda: len(order) == 3, timeout=2.0, msg=f"Expected [1, 2, 3], got {order}"
    )
    assert order == [1, 2, 3], f"Expected [1, 2, 3], got {order}"
    pool.shutdown()


def test_callback_fires_immediately_for_past_deadline() -> None:
    """A deadline in the past fires the callback on next pool cycle."""
    pool = _pool()
    fired: list[bool] = []
    past = time.monotonic() - 1.0  # already past

    pool.schedule(deadline=past, callback=lambda: fired.append(True))

    for _ in range(30):
        time.sleep(0.01)
        if fired:
            break

    assert fired, "Callback for past deadline was never fired"
    pool.shutdown()


# ---------------------------------------------------------------------------
# Adversarial
# ---------------------------------------------------------------------------


def test_double_cancel_is_noop() -> None:
    """Cancelling the same handle twice does not raise."""
    pool = _pool()
    handle = pool.schedule(deadline=time.monotonic() + 10.0, callback=lambda: None)
    pool.cancel(handle)
    pool.cancel(handle)  # Should not raise
    pool.shutdown()


def test_cancel_nonexistent_handle_is_noop() -> None:
    """Cancelling a handle that was never scheduled does not raise."""
    pool = _pool()
    pool.cancel(99999)  # Should not raise
    pool.shutdown()


def test_callback_exception_does_not_crash_pool() -> None:
    """A callback that raises must not kill the daemon thread."""
    pool = _pool()
    good_fired: list[int] = []
    now = time.monotonic()

    def _bad_callback() -> None:
        raise RuntimeError("deliberate callback error")

    pool.schedule(deadline=now + 0.05, callback=_bad_callback)
    pool.schedule(deadline=now + 0.15, callback=lambda: good_fired.append(1))

    wait_for(
        lambda: good_fired == [1],
        timeout=2.0,
        msg=f"Pool must continue after callback exception; got {good_fired}",
    )
    pool.shutdown()


def test_schedule_after_shutdown_raises() -> None:
    """schedule() raises RuntimeError after shutdown()."""
    pool = _pool()
    pool.shutdown()
    time.sleep(0.05)  # Let thread exit

    raised = False
    try:
        pool.schedule(deadline=time.monotonic() + 1.0, callback=lambda: None)
    except RuntimeError:
        raised = True

    assert raised, "schedule() after shutdown must raise RuntimeError"


def test_concurrent_schedules_no_deadlock() -> None:
    """50 threads each scheduling one callback -- no deadlock or crash."""
    pool = _pool()
    fired: list[int] = []
    lock = threading.Lock()
    now = time.monotonic()

    def _schedule(i: int) -> None:
        pool.schedule(
            deadline=now + 0.05,
            callback=lambda idx=i: (lock.acquire(), fired.append(idx), lock.release()),
        )

    threads = [threading.Thread(target=_schedule, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wait_for(
        lambda: len(fired) == 50,
        timeout=2.0,
        msg=f"Expected 50 callbacks fired, got {len(fired)}",
    )
    pool.shutdown()


# ---------------------------------------------------------------------------
# ExecutionContext integration
# ---------------------------------------------------------------------------


def test_execution_context_uses_pool_not_thread() -> None:
    """ExecutionContext with timeout_ms schedules via pool (no legacy thread)."""
    config = ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=50,
        max_retries_total=5,
        timeout_ms=500,
    )
    with ExecutionContext(config=config) as ctx:
        # Pool handle should be set; legacy thread should NOT be started
        assert ctx._timeout_pool_handle is not None, (
            "Pool handle must be set when timeout_ms > 0"
        )
        assert not hasattr(ctx, "_timeout_thread"), (
            "Legacy timeout_thread field must not exist (removed in simplify)"
        )


def test_execution_context_pool_cancel_on_exit_prevents_callback() -> None:
    """Pool callback must NOT fire after context exits early."""
    config = ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=50,
        max_retries_total=5,
        timeout_ms=500,  # 500ms -- long enough to not fire during test
    )
    # Exit context immediately (well before timeout)
    with ExecutionContext(config=config) as ctx:
        handle = ctx._timeout_pool_handle
        assert handle is not None

    # After __exit__, pool handle should be cancelled.
    # The token should still be cancelled (by __exit__), but via the abort
    # path, not the timeout path.
    assert ctx._cancellation_token.is_cancelled, (
        "CancellationToken must be cancelled after context exit"
    )
    # Pool handle cleared after cancellation
    assert ctx._timeout_pool_handle is None, "Pool handle must be cleared in __exit__"


def test_execution_context_timeout_still_works_via_pool() -> None:
    """Timeout fires correctly when routed through SharedTimeoutPool."""
    config = ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=50,
        max_retries_total=5,
        timeout_ms=150,
    )
    with ExecutionContext(config=config) as ctx:
        # Wait for the pool-routed timeout to fire before making the call
        wait_for(
            lambda: ctx._cancellation_token.is_cancelled,
            timeout=2.0,
            msg="Timeout via pool did not fire within 2s",
        )
        fn_called: list[bool] = []
        decision = ctx.wrap_llm_call(fn=lambda: fn_called.append(True))

    assert decision == Decision.HALT, (
        f"Expected HALT after pool-routed timeout, got {decision}"
    )
    assert fn_called == [], "fn must NOT be called after timeout"
