"""Adversarial tests for AgentStepGuard (agent_guard.py).

Covers code paths NOT already tested in test_runtime_policy.py:
- Boundary: step exactly at max_steps returns False
- step() at max_steps-1 returns True, at max_steps returns False
- Concurrent threads racing on step()
- last_result preservation under concurrent access
- reset() thread-safety
- check() partial_result matches last_result
- max_steps=1 edge case
- max_steps=0 edge case (immediately exceeded)
"""

from __future__ import annotations

import threading

import pytest

from veronica_core.agent_guard import AgentStepGuard
from veronica_core.runtime_policy import PolicyContext


class TestAgentStepBoundary:
    """Boundary conditions on step counting."""

    def test_step_returns_false_at_exactly_max_steps(self) -> None:
        g = AgentStepGuard(max_steps=3)
        g.step()
        g.step()
        result = g.step()  # 3rd step: current_step becomes 3 >= max_steps=3
        assert result is False

    def test_step_returns_true_one_before_limit(self) -> None:
        g = AgentStepGuard(max_steps=5)
        for _ in range(4):
            g.step()
        assert g.remaining_steps == 1
        result = g.step()  # 5th step hits limit
        assert result is False
        assert g.remaining_steps == 0

    def test_max_steps_one_first_step_denies(self) -> None:
        """With max_steps=1, the very first step should deny."""
        g = AgentStepGuard(max_steps=1)
        result = g.step()
        assert result is False
        assert g.is_exceeded

    def test_max_steps_zero_immediately_exceeded(self) -> None:
        """With max_steps=0, check() immediately returns denied."""
        g = AgentStepGuard(max_steps=0)
        decision = g.check(PolicyContext())
        assert not decision.allowed

    def test_is_exceeded_false_before_limit(self) -> None:
        g = AgentStepGuard(max_steps=10)
        for _ in range(9):
            g.step()
        assert not g.is_exceeded

    def test_is_exceeded_true_at_limit(self) -> None:
        g = AgentStepGuard(max_steps=3)
        g.step()
        g.step()
        g.step()
        assert g.is_exceeded

    def test_remaining_steps_never_negative(self) -> None:
        g = AgentStepGuard(max_steps=2)
        g.step()
        g.step()
        g.step()  # Over limit
        assert g.remaining_steps == 0


class TestAgentStepResultPreservation:
    """last_result must be preserved correctly."""

    def test_last_result_none_when_no_result_given(self) -> None:
        g = AgentStepGuard(max_steps=5)
        g.step()
        assert g.last_result is None

    def test_last_result_updated_only_when_result_not_none(self) -> None:
        g = AgentStepGuard(max_steps=5)
        g.step(result="first")
        g.step()  # No result passed
        assert g.last_result == "first"

    def test_last_result_at_limit_preserved_in_check(self) -> None:
        g = AgentStepGuard(max_steps=2)
        g.step(result="intermediate")
        g.step(result="final")
        decision = g.check(PolicyContext())
        assert not decision.allowed
        assert decision.partial_result == "final"

    def test_last_result_complex_object(self) -> None:
        g = AgentStepGuard(max_steps=3)
        payload = {"output": [1, 2, 3], "tokens": 42}
        g.step(result=payload)
        assert g.last_result is payload  # Same object reference

    def test_reset_clears_last_result(self) -> None:
        g = AgentStepGuard(max_steps=3)
        g.step(result="something")
        g.reset()
        assert g.last_result is None
        assert g.current_step == 0


class TestAgentStepConcurrency:
    """Thread-safety: concurrent step() calls must not allow over-stepping."""

    def test_concurrent_steps_total_at_most_max_steps_true(self) -> None:
        """With 100 threads and max_steps=10, exactly max_steps-1=9 return True.

        step() increments first then checks `current_step >= max_steps`:
        steps 1-9 → True, step 10 onwards → False.
        """
        max_steps = 10
        g = AgentStepGuard(max_steps=max_steps)
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(100)

        def do_step() -> None:
            barrier.wait()
            r = g.step()
            with lock:
                results.append(r)

        threads = [threading.Thread(target=do_step) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # steps 1..max_steps-1 return True, step max_steps and beyond return False
        expected_true = max_steps - 1
        assert sum(results) == expected_true
        assert len([r for r in results if not r]) == 100 - expected_true

    def test_concurrent_step_current_step_consistent(self) -> None:
        """current_step must exactly equal number of step() calls."""
        g = AgentStepGuard(max_steps=1000)
        barrier = threading.Barrier(50)

        def do_step() -> None:
            barrier.wait()
            g.step()

        threads = [threading.Thread(target=do_step) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert g.current_step == 50

    def test_concurrent_reset_and_step_no_crash(self) -> None:
        """reset() and step() interleaved must not cause exceptions."""
        g = AgentStepGuard(max_steps=100)
        errors: list[Exception] = []

        def do_steps() -> None:
            try:
                for _ in range(20):
                    g.step()
            except Exception as exc:
                errors.append(exc)

        def do_reset() -> None:
            try:
                for _ in range(5):
                    g.reset()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_steps) for _ in range(5)] + [
            threading.Thread(target=do_reset) for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


class TestAgentStepCheckConcurrency:
    """check() must be safe to call concurrently with step()."""

    def test_concurrent_check_and_step_no_crash(self) -> None:
        g = AgentStepGuard(max_steps=50)
        errors: list[Exception] = []

        def do_step() -> None:
            try:
                for _ in range(10):
                    g.step()
            except Exception as exc:
                errors.append(exc)

        def do_check() -> None:
            try:
                for _ in range(10):
                    g.check(PolicyContext())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_step) for _ in range(5)] + [
            threading.Thread(target=do_check) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []



class TestAgentStepGuardValidation:
    """Adversarial: __post_init__ rejects invalid max_steps."""

    def test_bool_true_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="max_steps must be an int"):
            AgentStepGuard(max_steps=True)

    def test_bool_false_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="max_steps must be an int"):
            AgentStepGuard(max_steps=False)

    def test_negative_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            AgentStepGuard(max_steps=-1)

    def test_zero_is_valid(self) -> None:
        guard = AgentStepGuard(max_steps=0)
        assert guard.max_steps == 0
        assert guard.step() is False  # immediately at limit
