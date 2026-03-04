"""Adversarial tests for BudgetEnforcer (budget.py).

Covers code paths NOT already tested in test_runtime_policy.py:
- NaN / Inf / negative spend() inputs
- Concurrent thread races on spend()
- zero-limit utilization (inf)
- check() with invalid cost_usd values
- is_exceeded gate in check()
- remaining_usd after exceeded flag
- to_dict() consistency under concurrent spend
"""

from __future__ import annotations

import threading

import pytest

from veronica_core.budget import BudgetEnforcer
from veronica_core.runtime_policy import PolicyContext


class TestBudgetSpendInvalidInputs:
    """spend() must raise ValueError for non-finite or negative amounts."""

    @pytest.mark.parametrize("bad_amount", [
        float("nan"),
        float("inf"),
        float("-inf"),
        -0.01,
        -100.0,
    ])
    def test_spend_raises_on_invalid_amount(self, bad_amount: float) -> None:
        b = BudgetEnforcer(limit_usd=100.0)
        with pytest.raises(ValueError):
            b.spend(bad_amount)

    def test_spend_zero_is_allowed(self) -> None:
        b = BudgetEnforcer(limit_usd=10.0)
        result = b.spend(0.0)
        assert result is True
        assert b.spent_usd == 0.0
        assert b.call_count == 1

    def test_spend_invalid_does_not_corrupt_state(self) -> None:
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(3.0)
        with pytest.raises(ValueError):
            b.spend(float("nan"))
        # State must be unchanged after the failed spend
        assert b.spent_usd == 3.0
        assert b.call_count == 1
        assert not b.is_exceeded


class TestBudgetCheckInvalidCost:
    """check() must deny requests with invalid cost_usd in context."""

    @pytest.mark.parametrize("bad_cost", [
        float("nan"),
        float("inf"),
        float("-inf"),
        -1.0,
        -0.001,
    ])
    def test_check_denies_invalid_cost_usd(self, bad_cost: float) -> None:
        b = BudgetEnforcer(limit_usd=100.0)
        decision = b.check(PolicyContext(cost_usd=bad_cost))
        assert not decision.allowed
        assert decision.policy_type == "budget"
        assert "invalid" in decision.reason.lower()

    def test_check_nan_does_not_pass_silently(self) -> None:
        """NaN compares as False in > checks; we must explicitly reject it."""
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(9.99)  # Near limit
        decision = b.check(PolicyContext(cost_usd=float("nan")))
        # NaN must NOT silently pass -- it must be denied
        assert not decision.allowed


class TestBudgetExceededGate:
    """Once _exceeded=True, check() must deny without re-evaluating spend."""

    def test_check_denies_when_already_exceeded(self) -> None:
        b = BudgetEnforcer(limit_usd=5.0)
        b.spend(4.0)
        b.spend(3.0)  # This triggers exceeded (7 > 5)
        assert b.is_exceeded

        # Even a zero-cost check should be denied
        decision = b.check(PolicyContext(cost_usd=0.0))
        assert not decision.allowed
        assert "exceeded" in decision.reason.lower()

    def test_remaining_usd_is_zero_after_exceeded(self) -> None:
        b = BudgetEnforcer(limit_usd=5.0)
        b.spend(3.0)
        b.spend(4.0)  # Exceeds; spend returns False
        assert b.remaining_usd == 0.0

    def test_reset_clears_exceeded_flag(self) -> None:
        b = BudgetEnforcer(limit_usd=5.0)
        b.spend(3.0)
        b.spend(4.0)
        assert b.is_exceeded
        b.reset()
        assert not b.is_exceeded
        assert b.remaining_usd == 5.0


class TestBudgetUtilizationEdgeCases:
    """utilization property edge cases."""

    def test_utilization_zero_limit_returns_inf(self) -> None:
        b = BudgetEnforcer(limit_usd=0.0)
        assert b.utilization == float("inf")

    def test_utilization_exact_limit(self) -> None:
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(10.0)
        assert b.utilization == pytest.approx(1.0)

    def test_utilization_exceeds_one_when_over_budget(self) -> None:
        # spend returns False when over, so spent_usd stays at last valid value
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(9.0)
        result = b.spend(5.0)  # Rejected
        assert result is False
        # spent_usd was NOT incremented on rejection
        assert b.utilization == pytest.approx(0.9)


class TestBudgetConcurrentSpend:
    """Thread-safety: concurrent spend() calls must not corrupt total."""

    def test_concurrent_spend_total_is_consistent(self) -> None:
        """100 threads each spend $1.0; total must be exactly $50 (limit)."""
        b = BudgetEnforcer(limit_usd=50.0)
        results: list[bool] = []
        lock = threading.Lock()

        def spend_one() -> None:
            r = b.spend(1.0)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=spend_one) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 50 spends should succeed, 50 should be rejected
        assert sum(results) == 50
        assert b.spent_usd == pytest.approx(50.0)

    def test_concurrent_spend_no_double_counting(self) -> None:
        """Each approved spend is counted exactly once."""
        b = BudgetEnforcer(limit_usd=100.0)
        barrier = threading.Barrier(10)

        def spend_synchronized() -> None:
            barrier.wait()  # All threads enter simultaneously
            b.spend(1.0)

        threads = [threading.Thread(target=spend_synchronized) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert b.spent_usd == pytest.approx(10.0)
        assert b.call_count == 10

    def test_concurrent_check_does_not_modify_state(self) -> None:
        """check() must never increment spent_usd (read-only semantics)."""
        b = BudgetEnforcer(limit_usd=100.0)
        b.spend(10.0)
        baseline = b.spent_usd

        barrier = threading.Barrier(20)

        def check_only() -> None:
            barrier.wait()
            b.check(PolicyContext(cost_usd=1.0))

        threads = [threading.Thread(target=check_only) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert b.spent_usd == baseline  # Unchanged


class TestBudgetToDictConsistency:
    """to_dict() must reflect committed state."""

    def test_to_dict_reflects_spend(self) -> None:
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(3.5)
        b.spend(2.5)
        d = b.to_dict()
        assert d["limit_usd"] == 10.0
        assert d["spent_usd"] == pytest.approx(6.0)
        assert d["call_count"] == 2

    def test_to_dict_after_reset(self) -> None:
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(5.0)
        b.reset()
        d = b.to_dict()
        assert d["spent_usd"] == 0.0
        assert d["call_count"] == 0


# ---------------------------------------------------------------------------
# Constructor validation — NaN / Inf / negative limit_usd
# ---------------------------------------------------------------------------


class TestBudgetEnforcerConstructorValidation:
    """BudgetEnforcer.__post_init__ must reject invalid limit_usd."""

    def test_limit_usd_nan_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            BudgetEnforcer(limit_usd=float("nan"))

    def test_limit_usd_positive_inf_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            BudgetEnforcer(limit_usd=float("inf"))

    def test_limit_usd_negative_inf_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            BudgetEnforcer(limit_usd=float("-inf"))

    def test_limit_usd_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            BudgetEnforcer(limit_usd=-0.01)

    def test_limit_usd_zero_is_valid(self) -> None:
        b = BudgetEnforcer(limit_usd=0.0)
        assert b.limit_usd == 0.0
