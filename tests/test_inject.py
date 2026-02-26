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
        # max_steps=0 causes immediate denial on every call (step 0 >= max 0).
        @veronica_guard(max_cost_usd=1.0, max_steps=0)
        def my_func() -> str:
            return "should not reach"  # pragma: no cover

        with pytest.raises(VeronicaHalt) as exc_info:
            my_func()

        assert exc_info.value.reason  # non-empty denial reason
        assert isinstance(exc_info.value.decision, PolicyDecision)
        assert not exc_info.value.decision.allowed

    def test_halt_is_runtime_error(self) -> None:
        @veronica_guard(max_steps=0)
        def my_func() -> None:
            pass  # pragma: no cover

        with pytest.raises(RuntimeError):
            my_func()


# ---------------------------------------------------------------------------
# Test: return_decision path
# ---------------------------------------------------------------------------

class TestReturnDecision:
    def test_returns_policy_decision_on_deny(self) -> None:
        # max_steps=0 triggers immediate denial so return_decision path is exercised.
        @veronica_guard(max_steps=0, return_decision=True)
        def my_func() -> str:
            return "result"  # pragma: no cover

        result = my_func()

        assert isinstance(result, PolicyDecision)
        assert not result.allowed
        assert result.reason  # non-empty denial reason

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

        # max_steps=0 on inner means it denies immediately on every call.
        @veronica_guard(max_steps=0)
        def inner() -> None:
            pass  # pragma: no cover

        @veronica_guard()
        def outer() -> None:
            inner()

        with pytest.raises(VeronicaHalt):
            outer()


# ---------------------------------------------------------------------------
# Test: timeout_ms backward-compat deprecation shim
# ---------------------------------------------------------------------------

class TestTimeoutMsDeprecation:
    """timeout_ms is accepted but deprecated; no TypeError, DeprecationWarning emitted."""

    def test_veronica_guard_timeout_ms_warns(self) -> None:
        with pytest.warns(DeprecationWarning, match="timeout_ms"):
            @veronica_guard(timeout_ms=5000)
            def my_func() -> int:
                return 1

        # Function must still be callable (no TypeError).
        assert my_func() == 1

    def test_veronica_guard_timeout_ms_none_no_warning(self) -> None:
        # No warning when timeout_ms is omitted (default None).
        import warnings as _warnings
        with _warnings.catch_warnings():
            _warnings.simplefilter("error", DeprecationWarning)

            @veronica_guard()
            def my_func() -> int:
                return 2

        assert my_func() == 2

    def test_guard_config_timeout_ms_warns(self) -> None:
        with pytest.warns(DeprecationWarning, match="timeout_ms"):
            cfg = GuardConfig(timeout_ms=3000)

        assert cfg.timeout_ms == 3000  # stored but ignored at runtime

    def test_guard_config_timeout_ms_none_no_warning(self) -> None:
        import warnings as _warnings
        with _warnings.catch_warnings():
            _warnings.simplefilter("error", DeprecationWarning)
            cfg = GuardConfig()

        assert cfg.timeout_ms is None

    def test_veronica_guard_timeout_ms_positional_compat(self) -> None:
        """Legacy positional 4th arg (timeout_ms) must NOT bind to return_decision.

        Before v1.0.0 the signature was:
            veronica_guard(max_cost_usd, max_steps, max_retries_total, timeout_ms)

        After the fix, parameter order is restored so that positional callers
        still have timeout_ms in slot 4 (index 3) and return_decision stays a
        keyword-only–style argument after it.
        """
        import warnings as _warnings
        with _warnings.catch_warnings():
            _warnings.simplefilter("always", DeprecationWarning)
            # Pass timeout_ms positionally (4th arg).  This must NOT make
            # veronica_guard behave as if return_decision=5000 (truthy),
            # which would cause it to return a PolicyDecision on denial
            # instead of raising VeronicaHalt.
            @veronica_guard(1.0, 0, 3, 5000)  # max_steps=0 → will deny
            def my_func() -> str:
                return "nope"  # pragma: no cover

        # Should raise VeronicaHalt, not return a PolicyDecision.
        with pytest.raises(VeronicaHalt):
            my_func()
