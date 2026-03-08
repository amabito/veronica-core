"""Tests for MemoryGovernor -- orchestrator for memory governance hooks.

Covers: fail-closed/fail-open, verdict aggregation, hook error handling,
        max cap enforcement, notify_after, and thread safety.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.hooks import (
    DefaultMemoryGovernanceHook,
    DenyAllMemoryGovernanceHook,
)
from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _op(action: MemoryAction = MemoryAction.READ) -> MemoryOperation:
    return MemoryOperation(action=action, resource_id="r", agent_id="a")


def _make_verdict_hook(verdict: GovernanceVerdict, policy_id: str = "test") -> Any:
    """Return a hook that always returns the given verdict."""

    class _VerdictHook:
        def before_op(self, operation: MemoryOperation, context: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
            return MemoryGovernanceDecision(
                verdict=verdict,
                reason=f"forced {verdict.value}",
                policy_id=policy_id,
                operation=operation,
            )

        def after_op(self, operation: MemoryOperation, decision: MemoryGovernanceDecision, result=None, error=None) -> None:
            pass

    return _VerdictHook()


def _make_raising_hook() -> Any:
    """Return a hook whose before_op raises an exception."""

    class _RaisingHook:
        def before_op(self, operation: MemoryOperation, context: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
            raise RuntimeError("hook exploded")

        def after_op(self, operation: MemoryOperation, decision: MemoryGovernanceDecision, result=None, error=None) -> None:
            pass

    return _RaisingHook()


def _make_raising_after_hook() -> Any:
    """Return a hook whose after_op raises -- used for notify_after swallow tests."""

    class _RaisingAfterHook:
        def before_op(self, operation: MemoryOperation, context: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
            return MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW, policy_id="ok")

        def after_op(self, operation: MemoryOperation, decision: MemoryGovernanceDecision, result=None, error=None) -> None:
            raise RuntimeError("after_op exploded")

    return _RaisingAfterHook()


# ---------------------------------------------------------------------------
# No-hooks behavior
# ---------------------------------------------------------------------------


class TestNoHooks:
    def test_no_hooks_fail_closed(self) -> None:
        """With no hooks and fail_closed=True, evaluate() must return DENY."""
        gov = MemoryGovernor(fail_closed=True)
        decision = gov.evaluate(_op())
        assert decision.verdict is GovernanceVerdict.DENY
        assert decision.denied is True

    def test_no_hooks_fail_open(self) -> None:
        """With no hooks and fail_closed=False, evaluate() must return ALLOW."""
        gov = MemoryGovernor(fail_closed=False)
        decision = gov.evaluate(_op())
        assert decision.verdict is GovernanceVerdict.ALLOW
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Single hook behavior
# ---------------------------------------------------------------------------


class TestSingleHook:
    def test_single_allow_hook(self) -> None:
        """A governor with one ALLOW hook must return ALLOW."""
        gov = MemoryGovernor()
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        decision = gov.evaluate(_op())
        assert decision.allowed is True
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_single_deny_hook(self) -> None:
        """A governor with one DENY hook must return DENY."""
        gov = MemoryGovernor()
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DENY))
        decision = gov.evaluate(_op())
        assert decision.denied is True

    def test_default_hook_as_hook(self) -> None:
        """DefaultMemoryGovernanceHook must work correctly as a registered hook."""
        gov = MemoryGovernor()
        gov.add_hook(DefaultMemoryGovernanceHook())
        decision = gov.evaluate(_op())
        assert decision.allowed is True

    def test_deny_all_hook_as_hook(self) -> None:
        """DenyAllMemoryGovernanceHook must work correctly as a registered hook."""
        gov = MemoryGovernor()
        gov.add_hook(DenyAllMemoryGovernanceHook())
        decision = gov.evaluate(_op())
        assert decision.denied is True


# ---------------------------------------------------------------------------
# Multiple hooks and verdict aggregation
# ---------------------------------------------------------------------------


class TestVerdictAggregation:
    def test_deny_stops_evaluation(self) -> None:
        """After a DENY, subsequent hooks must not be evaluated."""
        call_log: list[str] = []

        class _TrackingHook:
            def __init__(self, name: str, verdict: GovernanceVerdict) -> None:
                self._name = name
                self._verdict = verdict

            def before_op(self, operation: MemoryOperation, context: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                call_log.append(self._name)
                return MemoryGovernanceDecision(verdict=self._verdict, policy_id=self._name)

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor()
        gov.add_hook(_TrackingHook("first", GovernanceVerdict.DENY))
        gov.add_hook(_TrackingHook("second", GovernanceVerdict.ALLOW))

        decision = gov.evaluate(_op())
        assert decision.denied is True
        assert call_log == ["first"]  # second must not be called

    def test_quarantine_propagated(self) -> None:
        """QUARANTINE verdict must propagate when no DENY follows."""
        gov = MemoryGovernor()
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.QUARANTINE))
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        decision = gov.evaluate(_op())
        assert decision.verdict is GovernanceVerdict.QUARANTINE
        assert decision.allowed is True

    def test_degrade_propagated(self) -> None:
        """DEGRADE verdict must propagate when no higher-severity verdict follows."""
        gov = MemoryGovernor()
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DEGRADE))
        decision = gov.evaluate(_op())
        assert decision.verdict is GovernanceVerdict.DEGRADE
        assert decision.allowed is True

    def test_quarantine_beats_degrade(self) -> None:
        """QUARANTINE must win over DEGRADE in worst-verdict aggregation."""
        gov = MemoryGovernor()
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DEGRADE))
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.QUARANTINE))
        decision = gov.evaluate(_op())
        assert decision.verdict is GovernanceVerdict.QUARANTINE

    def test_degrade_then_quarantine_order_irrelevant(self) -> None:
        """Worst-verdict aggregation must be order-independent for QUARANTINE/DEGRADE."""
        gov1 = MemoryGovernor()
        gov1.add_hook(_make_verdict_hook(GovernanceVerdict.QUARANTINE))
        gov1.add_hook(_make_verdict_hook(GovernanceVerdict.DEGRADE))

        gov2 = MemoryGovernor()
        gov2.add_hook(_make_verdict_hook(GovernanceVerdict.DEGRADE))
        gov2.add_hook(_make_verdict_hook(GovernanceVerdict.QUARANTINE))

        assert gov1.evaluate(_op()).verdict is GovernanceVerdict.QUARANTINE
        assert gov2.evaluate(_op()).verdict is GovernanceVerdict.QUARANTINE


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestHookErrorHandling:
    def test_hook_error_fails_closed(self) -> None:
        """An exception in before_op must cause a DENY verdict (fail-closed)."""
        gov = MemoryGovernor()
        gov.add_hook(_make_raising_hook())
        decision = gov.evaluate(_op())
        assert decision.denied is True
        assert "hook error" in decision.reason

    def test_hook_error_after_allow_fails_closed(self) -> None:
        """An exception in before_op must return DENY even after prior ALLOWs."""
        gov = MemoryGovernor()
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        gov.add_hook(_make_raising_hook())
        decision = gov.evaluate(_op())
        assert decision.denied is True


# ---------------------------------------------------------------------------
# Hook cap
# ---------------------------------------------------------------------------


class TestHookCap:
    def test_add_hook_max_cap(self) -> None:
        """Adding more than 100 hooks must raise RuntimeError."""
        gov = MemoryGovernor()
        for _ in range(100):
            gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        assert gov.hook_count == 100
        with pytest.raises(RuntimeError, match="100"):
            gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))


# ---------------------------------------------------------------------------
# notify_after
# ---------------------------------------------------------------------------


class TestNotifyAfter:
    def test_notify_after_calls_all_hooks(self) -> None:
        """notify_after() must call after_op on all registered hooks."""
        call_count: list[int] = [0]

        class _CountingHook:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW, policy_id="ok")

            def after_op(self, op: MemoryOperation, decision: MemoryGovernanceDecision, result=None, error=None) -> None:
                call_count[0] += 1

        gov = MemoryGovernor()
        for _ in range(3):
            gov.add_hook(_CountingHook())

        op = _op()
        decision = MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW, policy_id="ok")
        gov.notify_after(op, decision)
        assert call_count[0] == 3

    def test_notify_after_swallows_errors(self) -> None:
        """notify_after() must not raise even when hooks raise."""
        gov = MemoryGovernor()
        gov.add_hook(_make_raising_after_hook())
        op = _op()
        decision = MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW, policy_id="ok")
        gov.notify_after(op, decision)  # must not raise


# ---------------------------------------------------------------------------
# Context defaults
# ---------------------------------------------------------------------------


class TestEvaluateContextDefault:
    def test_evaluate_with_none_context_creates_default(self) -> None:
        """evaluate() with context=None must work without raising."""
        gov = MemoryGovernor()
        gov.add_hook(DefaultMemoryGovernanceHook())
        op = _op(MemoryAction.WRITE)
        decision = gov.evaluate(op, context=None)
        assert decision.allowed is True

    def test_evaluate_with_explicit_context(self) -> None:
        """evaluate() with an explicit context must pass it to hooks."""
        received_context: list[MemoryPolicyContext] = []

        class _CapturingHook:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                if ctx is not None:
                    received_context.append(ctx)
                return MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW, policy_id="ok")

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor()
        gov.add_hook(_CapturingHook())
        op = _op()
        ctx = MemoryPolicyContext(operation=op, chain_id="explicit-chain")
        gov.evaluate(op, context=ctx)
        assert len(received_context) == 1
        assert received_context[0].chain_id == "explicit-chain"


# ---------------------------------------------------------------------------
# Thread safety (adversarial)
# ---------------------------------------------------------------------------


class TestConcurrentEvaluate:
    def test_concurrent_evaluate(self) -> None:
        """10 threads each performing 50 evaluations must not corrupt state."""
        gov = MemoryGovernor()
        gov.add_hook(DefaultMemoryGovernanceHook())

        results: list[bool] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(50):
                try:
                    op = _op(MemoryAction.READ)
                    decision = gov.evaluate(op)
                    with lock:
                        results.append(decision.allowed)
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected errors: {errors}"
        assert len(results) == 500
        assert all(results), "All evaluations should have returned ALLOW"

    def test_concurrent_add_and_evaluate(self) -> None:
        """Concurrent add_hook() and evaluate() must not deadlock or crash."""
        gov = MemoryGovernor(fail_closed=False)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def adder() -> None:
            for _ in range(20):
                try:
                    gov.add_hook(DefaultMemoryGovernanceHook())
                except RuntimeError:
                    # Cap hit is acceptable
                    pass
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)

        def evaluator() -> None:
            for _ in range(30):
                try:
                    gov.evaluate(_op())
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)

        threads = [threading.Thread(target=adder) for _ in range(3)] + [
            threading.Thread(target=evaluator) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected errors: {errors}"
