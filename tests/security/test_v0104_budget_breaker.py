"""Regression tests for v0.10.4 fixes:
- BudgetEnforcer.spend() atomic check-then-add (Fix 1.1 + 1.2)
- CircuitBreaker.bind_to_context() ownership guard (Fix 6-A)
"""

from __future__ import annotations

import threading

import pytest

from veronica_core.budget import BudgetEnforcer
from veronica_core.circuit_breaker import CircuitBreaker


# ---------------------------------------------------------------------------
# BudgetEnforcer tests
# ---------------------------------------------------------------------------


def test_concurrent_overspend() -> None:
    """Exactly 1 thread succeeds when N threads race to spend above the limit."""
    N = 20
    limit = 1.0
    amount = 0.6  # Any single spend fits; two do not
    budget = BudgetEnforcer(limit_usd=limit)

    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        result = budget.spend(amount)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = sum(1 for r in results if r)
    assert successes == 1, f"Expected exactly 1 success, got {successes}"
    assert budget.spent_usd <= limit, (
        f"spent_usd {budget.spent_usd:.4f} exceeds limit {limit:.4f}"
    )


def test_negative_spend_raises() -> None:
    """spend() with a negative amount raises ValueError."""
    budget = BudgetEnforcer(limit_usd=100.0)
    with pytest.raises(ValueError, match="non-negative"):
        budget.spend(-1.0)


# ---------------------------------------------------------------------------
# CircuitBreaker ownership tests
# ---------------------------------------------------------------------------


def test_breaker_sharing_raises() -> None:
    """Binding the same CircuitBreaker to two different ctx_ids raises RuntimeError."""
    breaker = CircuitBreaker()
    breaker.bind_to_context("ctx-A")
    with pytest.raises(RuntimeError, match="shared across contexts"):
        breaker.bind_to_context("ctx-B")


def test_breaker_same_context_ok() -> None:
    """Binding the same CircuitBreaker to the same ctx_id twice is idempotent."""
    breaker = CircuitBreaker()
    breaker.bind_to_context("ctx-A")
    breaker.bind_to_context("ctx-A")  # Should not raise


def test_breaker_per_context_ok() -> None:
    """Two separate CircuitBreaker instances bound to different contexts are independent."""
    breaker_a = CircuitBreaker(failure_threshold=3)
    breaker_b = CircuitBreaker(failure_threshold=3)

    breaker_a.bind_to_context("ctx-A")
    breaker_b.bind_to_context("ctx-B")

    # Drive breaker_a to OPEN; breaker_b should remain CLOSED
    for _ in range(3):
        breaker_a.record_failure()

    from veronica_core.circuit_breaker import CircuitState

    assert breaker_a.state == CircuitState.OPEN
    assert breaker_b.state == CircuitState.CLOSED
