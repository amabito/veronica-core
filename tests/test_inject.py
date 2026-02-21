"""Tests for veronica_core.inject — decorator-based execution boundary."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from veronica_core.inject import (
    GuardConfig,
    VeronicaHalt,
    is_guard_active,
    veronica_guard,
)
from veronica_core.runtime_policy import PolicyDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deny_decision(reason: str = "test denial") -> PolicyDecision:
    return PolicyDecision(allowed=False, policy_type="guard", reason=reason)


def _allow_decision() -> PolicyDecision:
    return PolicyDecision(allowed=True, policy_type="guard", reason="")


# ---------------------------------------------------------------------------
# Test: basic allow path
# ---------------------------------------------------------------------------

class TestAllowPath:
    def test_function_executes_and_returns_value(self) -> None:
        @veronica_guard(max_cost_usd=1.0, max_steps=100, max_retries_total=5)
        def add(x: int, y: int) -> int:
            return x + y

        assert add(2, 3) == 5

    def test_function_receives_args_and_kwargs(self) -> None:
        @veronica_guard()
        def greet(name: str, greeting: str = "Hello") -> str:
            return f"{greeting}, {name}"

        assert greet("World", greeting="Hi") == "Hi, World"

    def test_guard_active_is_true_inside_wrapped_call(self) -> None:
        seen: list[bool] = []

        @veronica_guard()
        def probe() -> None:
            seen.append(is_guard_active())

        assert not is_guard_active()
        probe()
        assert seen == [True]
        assert not is_guard_active()


# ---------------------------------------------------------------------------
# Test: deny path raises VeronicaHalt
# ---------------------------------------------------------------------------

class TestDenyRaises:
    def test_raises_veronica_halt_on_deny(self) -> None:
        @veronica_guard(max_cost_usd=1.0)
        def my_func() -> str:
            return "should not reach"

        my_func._container.check = MagicMock(return_value=_deny_decision("budget exceeded"))

        with pytest.raises(VeronicaHalt) as exc_info:
            my_func()

        assert exc_info.value.reason == "budget exceeded"
        assert isinstance(exc_info.value.decision, PolicyDecision)
        assert not exc_info.value.decision.allowed

    def test_halt_is_runtime_error(self) -> None:
        @veronica_guard()
        def my_func() -> None:
            pass

        my_func._container.check = MagicMock(return_value=_deny_decision())

        with pytest.raises(RuntimeError):
            my_func()


# ---------------------------------------------------------------------------
# Test: return_decision path
# ---------------------------------------------------------------------------

class TestReturnDecision:
    def test_returns_policy_decision_on_deny(self) -> None:
        @veronica_guard(return_decision=True)
        def my_func() -> str:
            return "result"

        deny = _deny_decision("step limit")
        my_func._container.check = MagicMock(return_value=deny)

        result = my_func()

        assert isinstance(result, PolicyDecision)
        assert not result.allowed
        assert result.reason == "step limit"

    def test_allow_still_executes_function(self) -> None:
        @veronica_guard(return_decision=True)
        def my_func() -> int:
            return 42

        assert my_func() == 42


# ---------------------------------------------------------------------------
# Test: nested decorated calls
# ---------------------------------------------------------------------------

class TestNestedGuard:
    def test_nested_calls_both_execute(self) -> None:
        @veronica_guard(max_cost_usd=5.0, max_steps=100)
        def inner(x: int) -> int:
            return x * 2

        @veronica_guard(max_cost_usd=5.0, max_steps=100)
        def outer(x: int) -> int:
            return inner(x) + 1

        assert outer(3) == 7

    def test_guard_active_inside_nested_call(self) -> None:
        inner_seen: list[bool] = []
        outer_seen: list[bool] = []

        @veronica_guard()
        def inner() -> None:
            inner_seen.append(is_guard_active())

        @veronica_guard()
        def outer() -> None:
            outer_seen.append(is_guard_active())
            inner()
            outer_seen.append(is_guard_active())  # still True after inner returns

        outer()
        assert inner_seen == [True]
        assert outer_seen == [True, True]
        assert not is_guard_active()  # reset after outer returns

    def test_deny_in_inner_does_not_affect_outer_state(self) -> None:
        """VeronicaHalt from inner propagates naturally; outer is not silently swallowed."""

        @veronica_guard()
        def inner() -> None:
            pass

        @veronica_guard()
        def outer() -> None:
            inner()

        inner._container.check = MagicMock(return_value=_deny_decision("inner denied"))

        with pytest.raises(VeronicaHalt, match="inner denied"):
            outer()
