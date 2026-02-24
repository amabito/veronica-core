"""Tests for LLM safety modules: budget, agent guard, partial result, retry."""

import pytest
import time

from veronica_core.budget import BudgetEnforcer
from veronica_core.agent_guard import AgentStepGuard
from veronica_core.partial import PartialResultBuffer
from veronica_core.retry import RetryContainer


# --- BudgetEnforcer ---


class TestBudgetEnforcer:
    """Test chain-level budget enforcement."""

    def test_within_budget(self):
        budget = BudgetEnforcer(limit_usd=10.0)
        assert budget.spend(3.0)
        assert budget.spend(3.0)
        assert budget.spend(3.0)
        assert not budget.is_exceeded
        assert budget.remaining_usd == pytest.approx(1.0)

    def test_budget_exceeded(self):
        budget = BudgetEnforcer(limit_usd=5.0)
        assert budget.spend(3.0)
        assert not budget.spend(3.0)  # 6.0 > 5.0
        assert budget.is_exceeded
        assert budget.remaining_usd == 0.0

    def test_call_count(self):
        budget = BudgetEnforcer(limit_usd=100.0)
        budget.spend(1.0)
        budget.spend(2.0)
        budget.spend(3.0)
        assert budget.call_count == 3
        assert budget.spent_usd == pytest.approx(6.0)

    def test_utilization(self):
        budget = BudgetEnforcer(limit_usd=100.0)
        budget.spend(50.0)
        assert budget.utilization == pytest.approx(0.5)

    def test_reset(self):
        budget = BudgetEnforcer(limit_usd=10.0)
        budget.spend(8.0)
        budget.reset()
        assert budget.spent_usd == 0.0
        assert budget.call_count == 0
        assert not budget.is_exceeded


# --- AgentStepGuard ---


class TestAgentStepGuard:
    """Test agent step limit enforcement."""

    def test_within_limit(self):
        guard = AgentStepGuard(max_steps=5)
        for i in range(4):
            assert guard.step(result=f"step_{i}")
        assert guard.remaining_steps == 1

    def test_limit_reached(self):
        guard = AgentStepGuard(max_steps=3)
        assert guard.step()  # 1
        assert guard.step()  # 2
        assert not guard.step()  # 3 = max
        assert guard.is_exceeded

    def test_partial_result_preserved(self):
        guard = AgentStepGuard(max_steps=2)
        guard.step(result="first")
        guard.step(result="second")
        assert guard.last_result == "second"
        assert guard.is_exceeded

    def test_reset(self):
        guard = AgentStepGuard(max_steps=3)
        guard.step()
        guard.step()
        guard.step()
        assert guard.is_exceeded
        guard.reset()
        assert not guard.is_exceeded
        assert guard.current_step == 0
        assert guard.last_result is None


# --- PartialResultBuffer ---


class TestPartialResultBuffer:
    """Test partial result preservation."""

    def test_append_and_get(self):
        buf = PartialResultBuffer()
        buf.append("Hello ")
        buf.append("world")
        assert buf.get_partial() == "Hello world"
        assert buf.chunk_count == 2

    def test_is_partial(self):
        buf = PartialResultBuffer()
        assert not buf.is_partial  # Empty
        buf.append("chunk")
        assert buf.is_partial  # Has data, not complete
        buf.mark_complete()
        assert not buf.is_partial  # Complete
        assert buf.is_complete

    def test_clear(self):
        buf = PartialResultBuffer()
        buf.append("data")
        buf.set_metadata("key", "value")
        buf.mark_complete()
        buf.clear()
        assert buf.get_partial() == ""
        assert buf.chunk_count == 0
        assert not buf.is_complete
        assert buf.metadata == {}


# --- RetryContainer ---


class TestRetryContainer:
    """Test budget-aware retry containment."""

    def test_success_no_retry(self):
        retry = RetryContainer(max_retries=3)
        result = retry.execute(lambda: "ok")
        assert result == "ok"
        assert retry.attempt_count == 1
        assert retry.total_retries == 0

    def test_success_after_retry(self):
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("transient")
            return "recovered"

        retry = RetryContainer(max_retries=3, backoff_base=0.01)
        result = retry.execute(flaky)
        assert result == "recovered"
        assert retry.attempt_count == 3
        assert retry.total_retries == 2

    def test_all_retries_exhausted(self):
        def always_fail():
            raise RuntimeError("permanent")

        retry = RetryContainer(max_retries=2, backoff_base=0.01)
        with pytest.raises(RuntimeError, match="permanent"):
            retry.execute(always_fail)

        assert retry.attempt_count == 3  # 1 initial + 2 retries
        assert retry.last_error is not None

    def test_total_retries_accumulate(self):
        call_count = 0

        def fail_once():
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 1:
                raise ConnectionError("odd fail")
            return "ok"

        retry = RetryContainer(max_retries=3, backoff_base=0.01)
        retry.execute(fail_once)  # 1 fail + 1 success
        call_count = 0
        retry.execute(fail_once)  # 1 fail + 1 success
        assert retry.total_retries == 2  # Accumulated
