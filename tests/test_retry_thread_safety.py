"""Thread-safety tests for RetryContainer."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from veronica_core.retry import RetryContainer
from veronica_core.runtime_policy import PolicyContext


class TestRetryContainerThreadSafety:
    """RetryContainer.execute() must not allow concurrent execution."""

    def test_execute_is_serialized(self):
        """Two concurrent execute() calls must not overlap."""
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

        # Because execute() holds the lock, only 1 slow_fn runs at a time
        assert max_concurrent[0] == 1, (
            f"Expected max 1 concurrent execution, got {max_concurrent[0]}"
        )

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
