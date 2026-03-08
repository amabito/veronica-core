"""Tests for BudgetTracker.

Covers initial state, mutations, limit checking, and concurrent access.
"""

from __future__ import annotations

import threading

import pytest

from veronica_core.containment._budget_tracker import BudgetTracker

_EPSILON = 1e-9


class TestBudgetTrackerBasic:
    def test_initial_zero(self) -> None:
        tracker = BudgetTracker()
        assert tracker.cost == 0.0

    def test_add(self) -> None:
        tracker = BudgetTracker()
        tracker.add(0.5)
        assert tracker.cost == pytest.approx(0.5)

    def test_add_multiple(self) -> None:
        tracker = BudgetTracker()
        tracker.add(0.1)
        tracker.add(0.2)
        assert tracker.cost == pytest.approx(0.3)

    def test_add_returning(self) -> None:
        tracker = BudgetTracker()
        tracker.add(0.3)
        total = tracker.add_returning(0.2)
        assert total == pytest.approx(0.5)
        assert tracker.cost == pytest.approx(0.5)

    def test_set(self) -> None:
        tracker = BudgetTracker()
        tracker.add(1.0)
        tracker.set(0.0)
        assert tracker.cost == 0.0

    def test_set_to_nonzero(self) -> None:
        tracker = BudgetTracker()
        tracker.set(2.5)
        assert tracker.cost == pytest.approx(2.5)


class TestBudgetTrackerCheck:
    def test_check_within_limit(self) -> None:
        tracker = BudgetTracker()
        tracker.add(0.5)
        # 0.5 + epsilon < 1.0 -- within limit
        assert tracker.check(max_cost=1.0, epsilon=_EPSILON) is None

    def test_check_exactly_at_limit(self) -> None:
        tracker = BudgetTracker()
        tracker.add(1.0)
        # 1.0 + epsilon >= 1.0 -- exceeded
        assert tracker.check(max_cost=1.0, epsilon=_EPSILON) == "budget_exceeded"

    def test_check_exceeded(self) -> None:
        tracker = BudgetTracker()
        tracker.add(1.5)
        assert tracker.check(max_cost=1.0, epsilon=_EPSILON) == "budget_exceeded"

    def test_check_with_epsilon_pushes_over_limit(self) -> None:
        tracker = BudgetTracker()
        tracker.add(0.99999)
        # Without epsilon this would be under 1.0; with epsilon=0.001 it is over.
        assert tracker.check(max_cost=1.0, epsilon=0.001) == "budget_exceeded"

    def test_check_zero_cost_within_limit(self) -> None:
        tracker = BudgetTracker()
        assert tracker.check(max_cost=1.0, epsilon=_EPSILON) is None


class TestBudgetTrackerConcurrent:
    def test_concurrent_add(self) -> None:
        """10 threads each add 100 times -- total must equal 1000 * delta."""
        tracker = BudgetTracker()
        delta = 0.001
        n_threads = 10
        n_adds = 100
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(n_adds):
                    tracker.add(delta)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        expected = n_threads * n_adds * delta
        assert tracker.cost == pytest.approx(expected, rel=1e-6)

    def test_concurrent_add_returning_accumulates(self) -> None:
        """add_returning() under concurrency never loses increments."""
        tracker = BudgetTracker()
        results: list[float] = []
        lock = threading.Lock()
        n_threads = 5
        n_adds = 20

        def worker() -> None:
            for _ in range(n_adds):
                total = tracker.add_returning(1.0)
                with lock:
                    results.append(total)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert tracker.cost == pytest.approx(n_threads * n_adds)
        # The maximum reported total must equal the final accumulated value.
        assert max(results) == pytest.approx(n_threads * n_adds)
