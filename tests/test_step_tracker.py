"""Tests for StepTracker.

Covers initial state, mutations, limit checking, and concurrent access.
"""

from __future__ import annotations

import threading

from veronica_core.containment._step_tracker import StepTracker


class TestStepTrackerBasic:
    def test_initial_zero(self) -> None:
        tracker = StepTracker()
        assert tracker.count == 0

    def test_increment(self) -> None:
        tracker = StepTracker()
        tracker.increment()
        assert tracker.count == 1

    def test_increment_multiple(self) -> None:
        tracker = StepTracker()
        tracker.increment()
        tracker.increment()
        tracker.increment()
        assert tracker.count == 3

    def test_increment_returning(self) -> None:
        tracker = StepTracker()
        val = tracker.increment_returning()
        assert val == 1
        assert tracker.count == 1

    def test_increment_returning_sequential(self) -> None:
        tracker = StepTracker()
        assert tracker.increment_returning() == 1
        assert tracker.increment_returning() == 2
        assert tracker.increment_returning() == 3

    def test_set(self) -> None:
        tracker = StepTracker()
        tracker.increment()
        tracker.set(10)
        assert tracker.count == 10

    def test_set_to_zero(self) -> None:
        tracker = StepTracker()
        tracker.set(5)
        tracker.set(0)
        assert tracker.count == 0


class TestStepTrackerCheck:
    def test_check_within_limit(self) -> None:
        tracker = StepTracker()
        tracker.set(4)
        assert tracker.check(max_steps=5) is None

    def test_check_at_limit(self) -> None:
        tracker = StepTracker()
        tracker.set(5)
        assert tracker.check(max_steps=5) == "step_limit_exceeded"

    def test_check_exceeded(self) -> None:
        tracker = StepTracker()
        tracker.set(10)
        assert tracker.check(max_steps=5) == "step_limit_exceeded"

    def test_check_zero_count_within_limit(self) -> None:
        tracker = StepTracker()
        assert tracker.check(max_steps=1) is None


class TestStepTrackerConcurrent:
    def test_concurrent_increment(self) -> None:
        """10 threads each increment 100 times -- total must be exact."""
        tracker = StepTracker()
        n_threads = 10
        n_increments = 100
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

    def test_concurrent_increment_returning_no_duplicate(self) -> None:
        """Each increment_returning() call must return a unique value."""
        tracker = StepTracker()
        results: list[int] = []
        lock = threading.Lock()
        n_threads = 5
        n_increments = 20

        def worker() -> None:
            for _ in range(n_increments):
                val = tracker.increment_returning()
                with lock:
                    results.append(val)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All returned values must be unique (each thread got an exclusive slot).
        assert len(results) == len(set(results))
        assert max(results) == n_threads * n_increments
