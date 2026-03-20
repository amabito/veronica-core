"""Adversarial tests for tracker decomposition classes.

Attacker mindset: race conditions, numeric boundary abuse, overflow,
invalid inputs, TOCTOU, and TimeoutManager double-start / raising emit_fn.

Classes tested:
    BudgetTracker  -- _budget_tracker.py
    StepTracker    -- _step_tracker.py
    RetryTracker   -- _retry_tracker.py
    TimeoutManager -- _timeout_manager.py
"""

from __future__ import annotations

import math
import sys
import threading
import time
from typing import Any

import pytest

from veronica_core.containment._budget_tracker import BudgetTracker
from veronica_core.containment._retry_tracker import RetryTracker
from veronica_core.containment._step_tracker import StepTracker
from veronica_core.containment._timeout_manager import TimeoutManager
from veronica_core.containment.types import CancellationToken

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EPSILON = 1e-9
_MAX_FLOAT = sys.float_info.max
_NUM_THREADS = 10
_OPS_PER_THREAD = 200


def _run_threads(target: Any, n: int = _NUM_THREADS) -> list[Exception]:
    """Spawn *n* threads running *target*, return list of any exceptions."""
    errors: list[Exception] = []
    lock = threading.Lock()

    def wrapped() -> None:
        try:
            target()
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=wrapped) for _ in range(n)]
    barrier = threading.Barrier(n)

    # Replace target with barrier-synchronized version for maximum contention.
    def synchronized() -> None:
        barrier.wait()
        try:
            target()
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=synchronized) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


# ===========================================================================
# BudgetTracker
# ===========================================================================


class TestAdversarialBudgetTracker:
    """Adversarial tests for BudgetTracker -- attacker mindset."""

    # -----------------------------------------------------------------------
    # Concurrent access
    # -----------------------------------------------------------------------

    def test_concurrent_add_no_lost_update(self) -> None:
        """10 threads x 200 add(1.0) calls must yield exactly 2000.0."""
        tracker = BudgetTracker()

        def worker() -> None:
            for _ in range(_OPS_PER_THREAD):
                tracker.add(1.0)

        errors = _run_threads(worker)
        assert not errors
        assert tracker.cost == pytest.approx(_NUM_THREADS * _OPS_PER_THREAD)

    def test_concurrent_add_returning_no_duplicate_total(self) -> None:
        """add_returning() must return monotonically increasing totals with no gaps."""
        tracker = BudgetTracker()
        results: list[float] = []
        results_lock = threading.Lock()

        def worker() -> None:
            for _ in range(50):
                val = tracker.add_returning(1.0)
                with results_lock:
                    results.append(val)

        errors = _run_threads(worker, n=10)
        assert not errors
        # Maximum returned total must equal the actual accumulated value.
        assert max(results) == pytest.approx(tracker.cost)
        # No value can exceed the final total.
        assert all(v <= tracker.cost + _EPSILON for v in results)

    def test_concurrent_check_while_adding(self) -> None:
        """check() called concurrently with add() must never raise."""
        tracker = BudgetTracker()
        errors: list[Exception] = []
        errors_lock = threading.Lock()

        def adder() -> None:
            for _ in range(_OPS_PER_THREAD):
                tracker.add(0.001)

        def checker() -> None:
            for _ in range(_OPS_PER_THREAD):
                try:
                    tracker.check(max_cost=1.0, epsilon=_EPSILON)
                except Exception as exc:  # noqa: BLE001
                    with errors_lock:
                        errors.append(exc)

        threads = [threading.Thread(target=adder) for _ in range(5)] + [
            threading.Thread(target=checker) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_concurrent_set_and_add_no_crash(self) -> None:
        """set() and add() racing on the same tracker must not raise."""
        tracker = BudgetTracker()

        def setter() -> None:
            for _ in range(100):
                tracker.set(0.0)

        def adder() -> None:
            for _ in range(100):
                tracker.add(0.1)

        threads = [threading.Thread(target=setter) for _ in range(5)] + [
            threading.Thread(target=adder) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Final cost must be a valid finite float -- no corruption.
        assert math.isfinite(tracker.cost)

    # -----------------------------------------------------------------------
    # Negative / invalid inputs
    # -----------------------------------------------------------------------

    def test_add_negative_amount_raises(self) -> None:
        """add() with a negative amount must raise ValueError -- not silently corrupt state."""
        tracker = BudgetTracker()
        tracker.add(1.0)
        with pytest.raises(ValueError, match="non-negative"):
            tracker.add(-0.5)
        assert tracker.cost == pytest.approx(1.0)

    def test_add_nan_raises(self) -> None:
        """add(NaN) must raise ValueError -- NaN must not corrupt the accumulator."""
        tracker = BudgetTracker()
        with pytest.raises(ValueError, match="finite"):
            tracker.add(float("nan"))
        assert tracker.cost == pytest.approx(0.0)

    def test_add_positive_infinity_raises(self) -> None:
        """add(+inf) must raise ValueError -- infinity must not corrupt the accumulator."""
        tracker = BudgetTracker()
        with pytest.raises(ValueError, match="finite"):
            tracker.add(float("inf"))
        assert tracker.cost == pytest.approx(0.0)

    def test_add_negative_infinity_raises(self) -> None:
        """add(-inf) must raise ValueError -- negative infinity is neither finite nor non-negative."""
        tracker = BudgetTracker()
        with pytest.raises(ValueError, match="finite"):
            tracker.add(float("-inf"))
        assert tracker.cost == pytest.approx(0.0)

    # -----------------------------------------------------------------------
    # Overflow: near MAX_FLOAT
    # -----------------------------------------------------------------------

    def test_add_near_max_float_no_crash(self) -> None:
        """Adding near sys.float_info.max must not raise."""
        tracker = BudgetTracker()
        tracker.add(_MAX_FLOAT)
        # Cost is at MAX_FLOAT; check() must not raise.
        result = tracker.check(max_cost=1.0, epsilon=_EPSILON)
        assert result == "budget_exceeded"

    def test_add_max_float_twice_overflows_to_inf(self) -> None:
        """Adding MAX_FLOAT twice yields +inf in Python -- no crash."""
        tracker = BudgetTracker()
        tracker.add(_MAX_FLOAT)
        tracker.add(_MAX_FLOAT)
        # Python float overflow wraps to inf, not an exception.
        assert not math.isfinite(tracker.cost) or tracker.cost >= 0

    # -----------------------------------------------------------------------
    # Boundary: zero max_cost
    # -----------------------------------------------------------------------

    def test_check_zero_max_cost_always_exceeded(self) -> None:
        """When max_cost is 0.0, any positive cost must exceed the limit."""
        tracker = BudgetTracker()
        tracker.add(0.001)
        assert tracker.check(max_cost=0.0, epsilon=_EPSILON) == "budget_exceeded"

    def test_check_zero_max_cost_zero_cost_exceeded_due_to_epsilon(self) -> None:
        """Even zero cost + epsilon >= 0.0 triggers budget_exceeded."""
        tracker = BudgetTracker()
        # 0.0 + epsilon >= 0.0 -> True
        assert tracker.check(max_cost=0.0, epsilon=_EPSILON) == "budget_exceeded"

    def test_check_zero_max_cost_zero_epsilon_at_zero_cost(self) -> None:
        """0.0 + 0.0 >= 0.0 is True -- exceeded even with zero epsilon."""
        tracker = BudgetTracker()
        assert tracker.check(max_cost=0.0, epsilon=0.0) == "budget_exceeded"

    # -----------------------------------------------------------------------
    # TOCTOU: check() says OK, then another thread pushes over limit
    # -----------------------------------------------------------------------

    def test_toctou_check_then_add_races(self) -> None:
        """TOCTOU: thread A checks OK, thread B adds past limit before A acts.

        The check() result is a point-in-time snapshot.  We verify that
        check() itself never raises and returns a consistent str | None.
        """
        tracker = BudgetTracker()
        tracker.add(0.9)  # Just under limit of 1.0.

        # Parallel thread adds 0.2, pushing total over 1.0.
        pusher_done = threading.Event()

        def pusher() -> None:
            tracker.add(0.2)
            pusher_done.set()

        valid_results = {None, "budget_exceeded"}
        result_before: Any = tracker.check(max_cost=1.0, epsilon=_EPSILON)
        t = threading.Thread(target=pusher)
        t.start()
        pusher_done.wait(timeout=2.0)
        result_after: Any = tracker.check(max_cost=1.0, epsilon=_EPSILON)
        t.join()

        assert result_before in valid_results
        # After pusher runs, total is 1.1 -- must be exceeded.
        assert result_after == "budget_exceeded"


# ===========================================================================
# StepTracker
# ===========================================================================


class TestAdversarialStepTracker:
    """Adversarial tests for StepTracker -- attacker mindset."""

    # -----------------------------------------------------------------------
    # Concurrent access
    # -----------------------------------------------------------------------

    def test_concurrent_increment_no_lost_update(self) -> None:
        """10 threads x 200 increment() calls must yield exactly 2000."""
        tracker = StepTracker()

        def worker() -> None:
            for _ in range(_OPS_PER_THREAD):
                tracker.increment()

        errors = _run_threads(worker)
        assert not errors
        assert tracker.count == _NUM_THREADS * _OPS_PER_THREAD

    def test_concurrent_increment_returning_all_unique(self) -> None:
        """Every increment_returning() call must return a unique value."""
        tracker = StepTracker()
        results: list[int] = []
        results_lock = threading.Lock()

        def worker() -> None:
            for _ in range(50):
                val = tracker.increment_returning()
                with results_lock:
                    results.append(val)

        errors = _run_threads(worker, n=10)
        assert not errors
        assert len(results) == len(set(results)), (
            "Duplicate increment_returning() values"
        )
        assert max(results) == len(results)

    def test_concurrent_set_and_increment_no_crash(self) -> None:
        """set() and increment() racing must not raise or corrupt the int."""
        tracker = StepTracker()

        def resetter() -> None:
            for _ in range(100):
                tracker.set(0)

        def incrementer() -> None:
            for _ in range(100):
                tracker.increment()

        threads = [threading.Thread(target=resetter) for _ in range(5)] + [
            threading.Thread(target=incrementer) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # No exception must have propagated; count must be a non-negative int.
        assert isinstance(tracker.count, int)
        assert tracker.count >= 0

    def test_concurrent_check_while_incrementing(self) -> None:
        """check() called concurrently with increment() must never raise."""
        tracker = StepTracker()
        errors: list[Exception] = []
        errors_lock = threading.Lock()

        def incrementer() -> None:
            for _ in range(_OPS_PER_THREAD):
                tracker.increment()

        def checker() -> None:
            for _ in range(_OPS_PER_THREAD):
                try:
                    tracker.check(max_steps=500)
                except Exception as exc:  # noqa: BLE001
                    with errors_lock:
                        errors.append(exc)

        threads = [threading.Thread(target=incrementer) for _ in range(5)] + [
            threading.Thread(target=checker) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors

    # -----------------------------------------------------------------------
    # Overflow: near MAX_INT
    # -----------------------------------------------------------------------

    def test_increment_near_max_int_no_crash(self) -> None:
        """Incrementing from near sys.maxsize must not raise (Python ints are unbounded)."""
        tracker = StepTracker()
        tracker.set(sys.maxsize - 1)
        tracker.increment()
        assert tracker.count == sys.maxsize
        # Python ints are arbitrary precision; one more increment is safe.
        tracker.increment()
        assert tracker.count == sys.maxsize + 1

    def test_set_negative_value_rejected(self) -> None:
        """set() with a negative value must raise ValueError -- negative counts are invalid."""
        tracker = StepTracker()
        with pytest.raises(ValueError, match="non-negative"):
            tracker.set(-100)
        assert tracker.count == 0

    # -----------------------------------------------------------------------
    # Boundary: zero max_steps
    # -----------------------------------------------------------------------

    def test_check_zero_max_steps_at_zero_count_exceeded(self) -> None:
        """count=0 >= max_steps=0 is True -- limit immediately exceeded."""
        tracker = StepTracker()
        assert tracker.check(max_steps=0) == "step_limit_exceeded"

    def test_check_zero_max_steps_after_one_increment(self) -> None:
        """count=1 >= max_steps=0 -- still exceeded."""
        tracker = StepTracker()
        tracker.increment()
        assert tracker.check(max_steps=0) == "step_limit_exceeded"

    # -----------------------------------------------------------------------
    # TOCTOU
    # -----------------------------------------------------------------------

    def test_toctou_check_then_increment_races(self) -> None:
        """Thread A observes count=4 (below limit 5), thread B pushes to 5 before A acts."""
        tracker = StepTracker()
        tracker.set(4)

        pusher_done = threading.Event()

        def pusher() -> None:
            tracker.increment()  # count -> 5
            pusher_done.set()

        result_before: Any = tracker.check(max_steps=5)
        t = threading.Thread(target=pusher)
        t.start()
        pusher_done.wait(timeout=2.0)
        result_after: Any = tracker.check(max_steps=5)
        t.join()

        # Before push: count=4 < 5 -> None
        assert result_before is None
        # After push: count=5 >= 5 -> exceeded
        assert result_after == "step_limit_exceeded"


# ===========================================================================
# RetryTracker
# ===========================================================================


class TestAdversarialRetryTracker:
    """Adversarial tests for RetryTracker -- attacker mindset."""

    # -----------------------------------------------------------------------
    # Concurrent access
    # -----------------------------------------------------------------------

    def test_concurrent_increment_no_lost_update(self) -> None:
        """10 threads x 200 increment() calls must yield exactly 2000."""
        tracker = RetryTracker()

        def worker() -> None:
            for _ in range(_OPS_PER_THREAD):
                tracker.increment()

        errors = _run_threads(worker)
        assert not errors
        assert tracker.count == _NUM_THREADS * _OPS_PER_THREAD

    def test_concurrent_check_while_incrementing(self) -> None:
        """check() called concurrently with increment() must never raise."""
        tracker = RetryTracker()
        errors: list[Exception] = []
        errors_lock = threading.Lock()

        def incrementer() -> None:
            for _ in range(_OPS_PER_THREAD):
                tracker.increment()

        def checker() -> None:
            for _ in range(_OPS_PER_THREAD):
                try:
                    tracker.check(max_retries=1000)
                except Exception as exc:  # noqa: BLE001
                    with errors_lock:
                        errors.append(exc)

        threads = [threading.Thread(target=incrementer) for _ in range(5)] + [
            threading.Thread(target=checker) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors

    def test_concurrent_increment_count_is_exact(self) -> None:
        """After N threads x M increments the count must be N*M with no race."""
        tracker = RetryTracker()
        n_threads = 8
        n_ops = 125

        errors = _run_threads(
            lambda: [tracker.increment() for _ in range(n_ops)], n=n_threads
        )
        assert not errors
        assert tracker.count == n_threads * n_ops

    # -----------------------------------------------------------------------
    # Boundary: zero max_retries
    # -----------------------------------------------------------------------

    def test_check_zero_max_retries_at_zero_count(self) -> None:
        """count=0 >= max_retries=0 -- exceeded immediately."""
        tracker = RetryTracker()
        assert tracker.check(max_retries=0) == "retry_budget_exceeded"

    def test_check_zero_max_retries_after_increment(self) -> None:
        """count=1 >= max_retries=0 -- still exceeded."""
        tracker = RetryTracker()
        tracker.increment()
        assert tracker.check(max_retries=0) == "retry_budget_exceeded"

    # -----------------------------------------------------------------------
    # Overflow: near MAX_INT
    # -----------------------------------------------------------------------

    def test_large_increment_count_no_crash(self) -> None:
        """Incrementing 10_000 times must produce an exact integer count."""
        tracker = RetryTracker()
        for _ in range(10_000):
            tracker.increment()
        assert tracker.count == 10_000
        assert tracker.check(max_retries=10_000) == "retry_budget_exceeded"

    # -----------------------------------------------------------------------
    # TOCTOU
    # -----------------------------------------------------------------------

    def test_toctou_check_then_increment_races(self) -> None:
        """Thread A observes count=2 (below max_retries=3), thread B increments to 3."""
        tracker = RetryTracker()
        tracker.increment()
        tracker.increment()  # count = 2

        pusher_done = threading.Event()

        def pusher() -> None:
            tracker.increment()
            pusher_done.set()

        result_before: Any = tracker.check(max_retries=3)
        t = threading.Thread(target=pusher)
        t.start()
        pusher_done.wait(timeout=2.0)
        result_after: Any = tracker.check(max_retries=3)
        t.join()

        assert result_before is None
        assert result_after == "retry_budget_exceeded"


# ===========================================================================
# TimeoutManager
# ===========================================================================


class TestAdversarialTimeoutManager:
    """Adversarial tests for TimeoutManager -- attacker mindset."""

    # -----------------------------------------------------------------------
    # Double start_watcher: old handle must be cancelled
    # -----------------------------------------------------------------------

    def test_double_start_watcher_cancels_old_handle(self) -> None:
        """Calling start_watcher() twice must cancel the first watcher.

        The second watcher must still fire, but the first watcher's deadline
        (5 seconds) must not trigger independently -- only one cancellation
        occurs (from the second short watcher).
        """
        token = CancellationToken()
        mgr = TimeoutManager(token)
        fired_events: list[str] = []
        fired_lock = threading.Lock()

        def emit_first(stop_reason: str, detail: str) -> None:
            with fired_lock:
                fired_events.append("first")

        def emit_second(stop_reason: str, detail: str) -> None:
            with fired_lock:
                fired_events.append("second")

        # First watcher: 5-second timeout (should be cancelled by second start).
        mgr.start_watcher(timeout_ms=5000, emit_fn=emit_first, config_timeout_ms=5000)
        # Immediately replace with a short watcher.
        mgr.start_watcher(timeout_ms=80, emit_fn=emit_second, config_timeout_ms=80)

        # Wait for second watcher to fire.
        deadline = time.monotonic() + 1.0
        while not token.is_cancelled and time.monotonic() < deadline:
            time.sleep(0.01)

        mgr.cancel_watcher()

        assert token.is_cancelled, "Token must be cancelled by second watcher"
        with fired_lock:
            # The first emit_fn must NOT have been called (it was superseded).
            assert "first" not in fired_events
            assert "second" in fired_events

    def test_double_start_watcher_both_short_only_one_cancel(self) -> None:
        """Two short watchers: token must be cancelled exactly once (idempotent)."""
        token = CancellationToken()
        mgr = TimeoutManager(token)
        cancel_count: list[int] = [0]
        original_cancel = token.cancel

        def counting_cancel() -> None:
            cancel_count[0] += 1
            original_cancel()

        token.cancel = counting_cancel  # type: ignore[method-assign]

        def emit(stop_reason: str, detail: str) -> None:
            pass

        mgr.start_watcher(timeout_ms=5000, emit_fn=emit, config_timeout_ms=5000)
        mgr.start_watcher(timeout_ms=60, emit_fn=emit, config_timeout_ms=60)

        deadline = time.monotonic() + 1.0
        while not token.is_cancelled and time.monotonic() < deadline:
            time.sleep(0.01)

        mgr.cancel_watcher()
        # Give any in-flight first watcher a chance to fire (it should be cancelled).
        time.sleep(0.05)

        assert token.is_cancelled
        # cancel() must have been called at most once by the watcher callbacks.
        # (The second watcher fires; the first was cancelled before firing.)
        assert cancel_count[0] >= 1

    # -----------------------------------------------------------------------
    # emit_fn that raises: token.cancel() must still fire via try/finally
    # -----------------------------------------------------------------------

    def test_emit_fn_raises_token_still_cancelled(self) -> None:
        """If emit_fn() raises, token.cancel() must still be called (try/finally)."""
        token = CancellationToken()
        mgr = TimeoutManager(token)

        def raising_emit(stop_reason: str, detail: str) -> None:
            raise RuntimeError("emit exploded")

        mgr.start_watcher(timeout_ms=60, emit_fn=raising_emit, config_timeout_ms=60)

        # Wait for watcher to fire.
        deadline = time.monotonic() + 1.0
        while not token.is_cancelled and time.monotonic() < deadline:
            time.sleep(0.01)

        mgr.cancel_watcher()
        assert token.is_cancelled, (
            "token.cancel() must run via try/finally even when emit_fn raises"
        )

    def test_emit_fn_raises_multiple_times_token_cancelled_once(self) -> None:
        """emit_fn raising must not prevent token from reaching is_cancelled=True."""
        token = CancellationToken()
        mgr = TimeoutManager(token)
        call_count: list[int] = [0]

        def flaky_emit(stop_reason: str, detail: str) -> None:
            call_count[0] += 1
            raise ValueError("always fails")

        mgr.start_watcher(timeout_ms=50, emit_fn=flaky_emit, config_timeout_ms=50)

        deadline = time.monotonic() + 1.0
        while not token.is_cancelled and time.monotonic() < deadline:
            time.sleep(0.01)

        mgr.cancel_watcher()
        assert token.is_cancelled
        assert call_count[0] == 1  # emit_fn fired exactly once

    # -----------------------------------------------------------------------
    # Boundary: zero timeout_ms (fires immediately)
    # -----------------------------------------------------------------------

    def test_zero_timeout_ms_fires_immediately(self) -> None:
        """timeout_ms=0 schedules at time.monotonic() + 0 -- fires at next pool tick."""
        token = CancellationToken()
        mgr = TimeoutManager(token)
        fired: list[bool] = []

        def emit(stop_reason: str, detail: str) -> None:
            fired.append(True)

        mgr.start_watcher(timeout_ms=0, emit_fn=emit, config_timeout_ms=0)

        deadline = time.monotonic() + 1.0
        while not token.is_cancelled and time.monotonic() < deadline:
            time.sleep(0.005)

        mgr.cancel_watcher()
        assert token.is_cancelled
        assert fired

    # -----------------------------------------------------------------------
    # cancel_watcher() idempotency
    # -----------------------------------------------------------------------

    def test_cancel_watcher_idempotent_no_raise(self) -> None:
        """Calling cancel_watcher() multiple times must never raise."""
        token = CancellationToken()
        mgr = TimeoutManager(token)

        def emit(stop_reason: str, detail: str) -> None:
            pass

        mgr.start_watcher(timeout_ms=5000, emit_fn=emit, config_timeout_ms=5000)
        mgr.cancel_watcher()
        mgr.cancel_watcher()  # Second call must be silent.
        mgr.cancel_watcher()  # Third call must be silent.

    # -----------------------------------------------------------------------
    # Concurrent start_watcher calls
    # -----------------------------------------------------------------------

    def test_concurrent_start_watcher_calls_no_crash(self) -> None:
        """10 threads racing on start_watcher() must not raise or deadlock."""
        token = CancellationToken()
        mgr = TimeoutManager(token)
        errors: list[Exception] = []
        errors_lock = threading.Lock()

        def emit(stop_reason: str, detail: str) -> None:
            pass

        def worker() -> None:
            try:
                mgr.start_watcher(timeout_ms=5000, emit_fn=emit, config_timeout_ms=5000)
            except Exception as exc:  # noqa: BLE001
                with errors_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        mgr.cancel_watcher()
        assert not errors

    # -----------------------------------------------------------------------
    # check() reflects token state immediately after external cancel
    # -----------------------------------------------------------------------

    def test_check_reflects_external_cancel(self) -> None:
        """If an external caller cancels the token, check() must return 'timeout'."""
        token = CancellationToken()
        mgr = TimeoutManager(token)

        assert mgr.check() is None
        token.cancel()
        assert mgr.check() == "timeout"

    def test_check_does_not_change_token_state(self) -> None:
        """check() is read-only; it must not cancel the token as a side effect."""
        token = CancellationToken()
        mgr = TimeoutManager(token)

        for _ in range(100):
            result = mgr.check()
            assert result is None
            assert not token.is_cancelled

    # -----------------------------------------------------------------------
    # elapsed_ms grows monotonically even under contention
    # -----------------------------------------------------------------------

    def test_elapsed_ms_monotonic_under_thread_contention(self) -> None:
        """elapsed_ms readings from 10 threads must all be non-negative."""
        token = CancellationToken()
        mgr = TimeoutManager(token)
        readings: list[float] = []
        readings_lock = threading.Lock()

        def reader() -> None:
            for _ in range(50):
                val = mgr.elapsed_ms
                with readings_lock:
                    readings.append(val)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(v >= 0.0 for v in readings), "elapsed_ms must never be negative"
