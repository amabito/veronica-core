"""Tests for RetryTracker.

Covers initial state, mutations, limit checking, and concurrent access.
"""

from __future__ import annotations

import threading

from veronica_core.containment._retry_tracker import RetryTracker


class TestRetryTrackerBasic:
    def test_initial_zero(self) -> None:
        tracker = RetryTracker()
        assert tracker.count == 0

    def test_increment(self) -> None:
        tracker = RetryTracker()
        tracker.increment()
        assert tracker.count == 1

    def test_increment_multiple(self) -> None:
        tracker = RetryTracker()
        for _ in range(5):
            tracker.increment()
        assert tracker.count == 5


class TestRetryTrackerCheck:
    def test_check_within_budget(self) -> None:
        tracker = RetryTracker()
        tracker.increment()
        assert tracker.check(max_retries=3) is None

    def test_check_at_budget(self) -> None:
        tracker = RetryTracker()
        for _ in range(3):
            tracker.increment()
        assert tracker.check(max_retries=3) == "retry_budget_exceeded"

    def test_check_exceeded(self) -> None:
        tracker = RetryTracker()
        for _ in range(5):
            tracker.increment()
        assert tracker.check(max_retries=3) == "retry_budget_exceeded"

    def test_check_zero_retries_within_budget(self) -> None:
        tracker = RetryTracker()
        assert tracker.check(max_retries=1) is None


class TestRetryTrackerConcurrent:
    def test_concurrent_increment(self) -> None:
        """5 threads each increment 20 times -- total must be exact."""
        tracker = RetryTracker()
        n_threads = 5
        n_increments = 20
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(n_increments):
                    tracker.increment()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert tracker.count == n_threads * n_increments
