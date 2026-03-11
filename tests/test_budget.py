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

import math
import threading

import pytest

from veronica_core.budget import BudgetEnforcer
from veronica_core.runtime_policy import PolicyContext


class TestBudgetSpendInvalidInputs:
    """spend() must raise ValueError for non-finite or negative amounts."""

    @pytest.mark.parametrize(
        "bad_amount",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            -0.01,
            -100.0,
        ],
    )
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

    @pytest.mark.parametrize(
        "bad_cost",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            -1.0,
            -0.001,
        ],
    )
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


# ---------------------------------------------------------------------------
# Zero-budget adversarial tests
# ---------------------------------------------------------------------------


class TestBudgetZeroLimit:
    """Adversarial tests for limit_usd=0.0 edge case.

    A zero budget must block ALL calls -- even zero-cost ones.
    """

    def test_check_zero_cost_denied_on_zero_budget(self) -> None:
        """Zero-cost call on zero-budget must be DENIED."""
        b = BudgetEnforcer(limit_usd=0.0)
        decision = b.check(PolicyContext(cost_usd=0.0))
        assert not decision.allowed
        assert decision.policy_type == "budget"
        assert "zero" in decision.reason.lower()

    def test_check_nonzero_cost_denied_on_zero_budget(self) -> None:
        """Non-zero cost call on zero-budget must be DENIED."""
        b = BudgetEnforcer(limit_usd=0.0)
        decision = b.check(PolicyContext(cost_usd=0.001))
        assert not decision.allowed

    def test_utilization_zero_budget_is_one(self) -> None:
        """utilization for zero-budget must be 1.0 (100%), not inf."""
        b = BudgetEnforcer(limit_usd=0.0)
        assert b.utilization == 1.0
        assert math.isfinite(b.utilization)


class TestBudgetEnvelopeWiring:
    """DecisionEnvelope must be attached to all DENY paths in check()."""

    def test_zero_budget_deny_has_envelope(self) -> None:
        """Zero-budget DENY must carry a DecisionEnvelope."""
        b = BudgetEnforcer(limit_usd=0.0)
        decision = b.check(PolicyContext(cost_usd=0.0))
        assert not decision.allowed
        assert decision.envelope is not None
        assert decision.envelope.decision == "DENY"
        assert decision.envelope.reason_code == "BUDGET_EXCEEDED"
        assert decision.envelope.issuer == "BudgetEnforcer"
        assert decision.envelope.audit_id  # non-empty UUID4

    def test_exceeded_deny_has_envelope(self) -> None:
        """Already-exceeded DENY must carry a DecisionEnvelope."""
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(11.0)  # projected > limit -> _exceeded=True, returns False
        decision = b.check(PolicyContext(cost_usd=1.0))
        assert not decision.allowed
        assert decision.envelope is not None
        assert decision.envelope.decision == "DENY"
        assert decision.envelope.reason_code == "BUDGET_EXCEEDED"
        assert "exceeded" in decision.envelope.reason.lower()

    def test_would_exceed_deny_has_envelope(self) -> None:
        """Would-exceed DENY must carry a DecisionEnvelope."""
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(9.0)
        decision = b.check(PolicyContext(cost_usd=5.0))
        assert not decision.allowed
        assert decision.envelope is not None
        assert decision.envelope.decision == "DENY"
        assert decision.envelope.reason_code == "BUDGET_EXCEEDED"
        assert "would exceed" in decision.envelope.reason.lower()

    def test_invalid_cost_deny_has_envelope(self) -> None:
        """Invalid cost_usd DENY must carry a DecisionEnvelope with UNKNOWN reason code."""
        b = BudgetEnforcer(limit_usd=10.0)
        decision = b.check(PolicyContext(cost_usd=float("nan")))
        assert not decision.allowed
        assert decision.envelope is not None
        assert decision.envelope.decision == "DENY"
        assert decision.envelope.reason_code == "UNKNOWN"
        assert "invalid" in decision.envelope.reason.lower()

    def test_allow_has_no_envelope(self) -> None:
        """ALLOW decision must NOT carry an envelope (minimal wiring)."""
        b = BudgetEnforcer(limit_usd=100.0)
        decision = b.check(PolicyContext(cost_usd=1.0))
        assert decision.allowed
        assert decision.envelope is None

    def test_envelope_audit_id_is_unique_per_deny(self) -> None:
        """Each DENY must produce a unique audit_id."""
        b = BudgetEnforcer(limit_usd=0.0)
        d1 = b.check(PolicyContext(cost_usd=0.0))
        d2 = b.check(PolicyContext(cost_usd=0.0))
        assert d1.envelope is not None
        assert d2.envelope is not None
        assert d1.envelope.audit_id != d2.envelope.audit_id

    def test_envelope_reason_matches_decision_reason(self) -> None:
        """Envelope reason must match the PolicyDecision reason."""
        b = BudgetEnforcer(limit_usd=0.0)
        decision = b.check(PolicyContext(cost_usd=1.0))
        assert decision.envelope is not None
        assert decision.envelope.reason == decision.reason


class TestAdversarialBudgetEnvelope:
    """Adversarial tests for DecisionEnvelope wiring -- attacker mindset."""

    def test_concurrent_deny_envelopes_no_mixup(self) -> None:
        """10 threads hitting DENY must each get a distinct envelope."""
        b = BudgetEnforcer(limit_usd=0.0)
        results: list = []

        def check_deny() -> None:
            d = b.check(PolicyContext(cost_usd=1.0))
            results.append(d)

        threads = [threading.Thread(target=check_deny) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        audit_ids = [r.envelope.audit_id for r in results]
        assert len(set(audit_ids)) == 10  # all unique

        for r in results:
            assert not r.allowed
            assert r.envelope is not None
            assert r.envelope.decision == "DENY"
            assert r.envelope.issuer == "BudgetEnforcer"

    def test_envelope_is_frozen_immutable(self) -> None:
        """Returned envelope must be frozen -- mutation raises."""
        b = BudgetEnforcer(limit_usd=0.0)
        decision = b.check(PolicyContext(cost_usd=0.0))
        envelope = decision.envelope
        assert envelope is not None
        with pytest.raises(AttributeError):
            envelope.decision = "ALLOW"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            envelope.reason_code = "TAMPERED"  # type: ignore[misc]
        with pytest.raises(TypeError):
            envelope.metadata["injected"] = "evil"  # type: ignore[index]

    def test_envelope_survives_pipeline_propagation(self) -> None:
        """Envelope must survive through PolicyPipeline.evaluate()."""
        from veronica_core.runtime_policy import PolicyPipeline

        b = BudgetEnforcer(limit_usd=0.0)
        pipeline = PolicyPipeline([b])
        decision = pipeline.evaluate(PolicyContext(cost_usd=1.0))
        assert not decision.allowed
        assert decision.envelope is not None
        assert decision.envelope.decision == "DENY"
        assert decision.envelope.issuer == "BudgetEnforcer"

    def test_envelope_to_audit_dict_serializable(self) -> None:
        """Envelope.to_audit_dict() must return JSON-serializable dict."""
        import json

        b = BudgetEnforcer(limit_usd=0.0)
        decision = b.check(PolicyContext(cost_usd=0.0))
        assert decision.envelope is not None
        audit = decision.envelope.to_audit_dict()
        # Must not raise
        serialized = json.dumps(audit)
        parsed = json.loads(serialized)
        assert parsed["decision"] == "DENY"
        assert parsed["issuer"] == "BudgetEnforcer"
        assert parsed["audit_id"] == decision.envelope.audit_id

    def test_spend_does_not_produce_envelope(self) -> None:
        """spend() returns bool, not PolicyDecision -- no envelope leakage."""
        b = BudgetEnforcer(limit_usd=10.0)
        result = b.spend(5.0)
        assert result is True
        assert not hasattr(result, "envelope")

        result_deny = b.spend(100.0)
        assert result_deny is False
        assert not hasattr(result_deny, "envelope")

    @pytest.mark.parametrize(
        "cost,expected_code",
        [
            (float("nan"), "UNKNOWN"),
            (float("inf"), "UNKNOWN"),
            (float("-inf"), "UNKNOWN"),
            (-1.0, "UNKNOWN"),
        ],
        ids=["nan", "inf", "-inf", "negative"],
    )
    def test_invalid_cost_uses_unknown_not_budget_exceeded(
        self, cost: float, expected_code: str
    ) -> None:
        """Invalid cost_usd must use UNKNOWN reason code, not BUDGET_EXCEEDED."""
        b = BudgetEnforcer(limit_usd=100.0)
        decision = b.check(PolicyContext(cost_usd=cost))
        assert not decision.allowed
        assert decision.envelope is not None
        assert decision.envelope.reason_code == expected_code

    def test_concurrent_mixed_allow_deny_envelope_correctness(self) -> None:
        """Mixed ALLOW/DENY under concurrency -- ALLOW must have no envelope."""
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(9.5)  # near limit
        results: list = []

        def check(cost: float) -> None:
            d = b.check(PolicyContext(cost_usd=cost))
            results.append(d)

        threads = []
        for i in range(10):
            cost = 0.01 if i % 2 == 0 else 5.0  # small vs over-limit
            threads.append(threading.Thread(target=check, args=(cost,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        for r in results:
            if r.allowed:
                assert r.envelope is None
            else:
                assert r.envelope is not None
                assert r.envelope.decision == "DENY"

    def test_envelope_timestamp_is_recent(self) -> None:
        """Envelope timestamp must be within last 5 seconds (not stale)."""
        import time

        b = BudgetEnforcer(limit_usd=0.0)
        before = time.time()
        decision = b.check(PolicyContext(cost_usd=0.0))
        after = time.time()
        assert decision.envelope is not None
        assert before <= decision.envelope.timestamp <= after
