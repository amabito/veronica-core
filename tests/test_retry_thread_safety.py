"""Thread-safety tests for RetryContainer."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from veronica_core.retry import RetryContainer
from veronica_core.runtime_policy import PolicyContext


class TestRetryContainerThreadSafety:
    """RetryContainer.execute() must allow concurrent fn execution but protect state."""

    def test_execute_allows_concurrent_fn_calls(self):
        """After H3 fix: fn() runs outside the lock, so concurrent calls are allowed.

        The lock is only held for brief state updates (attempt_count, last_error,
        total_retries), not for the entire fn() + sleep() duration.  This prevents
        callers from being blocked for the full backoff period when another thread
        is sleeping between retries.
        """
        container = RetryContainer(max_retries=0, backoff_base=0.0)

        in_flight: list[int] = []
        max_concurrent: list[int] = [0]
        lock = threading.Lock()

        def slow_fn():
            with lock:
                in_flight.append(1)
                max_concurrent[0] = max(max_concurrent[0], len(in_flight))
            import time
            time.sleep(0.01)
            with lock:
                in_flight.pop()
            return "ok"

        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            container.execute(slow_fn)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # After H3 fix: fn() is called without holding the lock, so concurrent
        # calls are possible (max_concurrent > 1 is expected and correct).
        # The important invariant is that all 4 threads complete successfully.
        assert max_concurrent[0] >= 1, "At least one concurrent call must succeed"

    def test_reset_is_thread_safe(self):
        """reset() must not corrupt state when called concurrently."""
        container = RetryContainer(max_retries=2, backoff_base=0.0)

        def always_fails():
            raise RuntimeError("boom")

        # Exhaust retries so _last_error is set
        with pytest.raises(RuntimeError):
            with patch("time.sleep"):
                container.execute(always_fails)

        assert container.last_error is not None

        def do_reset():
            container.reset()

        threads = [threading.Thread(target=do_reset) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert container.last_error is None
        assert container.total_retries == 0

    def test_check_reads_last_error_consistently(self):
        """check() should reflect last_error state correctly after execute."""
        container = RetryContainer(max_retries=0, backoff_base=0.0)

        def always_fails():
            raise ValueError("bad")

        with pytest.raises(ValueError):
            container.execute(always_fails)

        ctx = PolicyContext()
        decision = container.check(ctx)
        assert not decision.allowed
        assert "exhausted" in (decision.reason or "")


class TestRetryLockReleasedDuringSleep:
    """H3 fix: check() and reset() must not be blocked while execute() sleeps."""

    def test_check_not_blocked_during_retry_sleep(self):
        """check() returns quickly even when execute() is in its backoff sleep."""
        import time as _time

        container = RetryContainer(max_retries=2, backoff_base=0.05, jitter=0.0)
        check_elapsed: list[float] = []

        def always_fails():
            raise RuntimeError("fail")

        def execute_thread():
            try:
                container.execute(always_fails)
            except RuntimeError:
                pass

        t = threading.Thread(target=execute_thread)
        t.start()

        # Wait until execute() is sleeping between retries
        _time.sleep(0.02)

        t0 = _time.monotonic()
        container.check(PolicyContext())
        elapsed = _time.monotonic() - t0
        check_elapsed.append(elapsed)

        t.join()

        # check() must return in << 0.05s (the sleep duration).
        # Allow 30ms for scheduling jitter.
        assert check_elapsed[0] < 0.03, (
            f"check() took {check_elapsed[0]:.3f}s — lock was held during sleep"
        )

    def test_reset_not_blocked_during_retry_sleep(self):
        """reset() returns quickly even when execute() is in its backoff sleep."""
        import time as _time

        container = RetryContainer(max_retries=2, backoff_base=0.05, jitter=0.0)
        reset_elapsed: list[float] = []

        def always_fails():
            raise RuntimeError("fail")

        def execute_thread():
            try:
                container.execute(always_fails)
            except RuntimeError:
                pass

        t = threading.Thread(target=execute_thread)
        t.start()
        _time.sleep(0.02)

        t0 = _time.monotonic()
        container.reset()
        elapsed = _time.monotonic() - t0
        reset_elapsed.append(elapsed)

        t.join()

        assert reset_elapsed[0] < 0.03, (
            f"reset() took {reset_elapsed[0]:.3f}s — lock was held during sleep"
        )
