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
# Adversarial: IEEE special value inputs to spend()
# ---------------------------------------------------------------------------


class TestAdversarialBudgetEnforcer:
    """Adversarial tests for BudgetEnforcer -- attacker mindset.

    Focus: IEEE 754 special values (inf, nan, -inf) passed to spend().
    """

    def test_spend_positive_inf_raises_value_error(self) -> None:
        """spend(+inf) must raise ValueError -- infinite amounts are invalid."""
        budget = BudgetEnforcer(limit_usd=100.0)
        with pytest.raises(ValueError, match="finite"):
            budget.spend(float("inf"))
        assert budget.spent_usd == 0.0

    def test_spend_negative_inf_raises_value_error(self) -> None:
        """spend(-inf) must raise ValueError -- infinite amounts are invalid."""
        budget = BudgetEnforcer(limit_usd=100.0)
        with pytest.raises(ValueError, match="finite"):
            budget.spend(float("-inf"))

    def test_spend_nan_raises_value_error(self) -> None:
        """spend(nan) must raise ValueError to prevent state corruption.

        Previously nan bypassed the negative guard (nan < 0 is False)
        and corrupted _spent_usd, disabling the budget entirely.
        Fixed: explicit isnan/isinf check before negative guard.
        """
        budget = BudgetEnforcer(limit_usd=100.0)
        with pytest.raises(ValueError, match="finite"):
            budget.spend(float("nan"))
        # State must remain clean after rejected nan
        assert budget.spent_usd == 0.0
        assert budget.is_exceeded is False

    def test_spend_nan_no_longer_corrupts_subsequent_spends(self) -> None:
        """After nan rejection, budget continues to function correctly."""
        budget = BudgetEnforcer(limit_usd=1.0)
        with pytest.raises(ValueError):
            budget.spend(float("nan"))
        # Budget still works after rejected nan
        assert budget.spend(0.5) is True
        assert budget.spent_usd == 0.5
        assert budget.spend(0.6) is False  # 0.5 + 0.6 > 1.0

    def test_spend_zero_is_a_no_op_that_increments_call_count(self) -> None:
        """spend(0.0) is valid: no cost is recorded, returns True.

        The call_count increments to record that a zero-cost call occurred.
        spent_usd and remaining_usd are unchanged.
        """
        budget = BudgetEnforcer(limit_usd=50.0)
        result = budget.spend(0.0)
        assert result is True
        assert budget.spent_usd == 0.0
        assert budget.remaining_usd == 50.0
        assert budget.call_count == 1  # zero-cost call is still a call


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
