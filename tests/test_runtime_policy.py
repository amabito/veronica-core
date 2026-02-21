"""Tests for VERONICA v0.2 Runtime Policy Control API.

Validates:
- RuntimePolicy protocol conformance for all 4 primitives
- PolicyContext / PolicyDecision dataclass behavior
- PolicyPipeline AND-composition with short-circuit
- CircuitBreaker state machine (CLOSED -> OPEN -> HALF_OPEN -> CLOSED)
- Backward compatibility of v0.1 APIs
"""

import time

import pytest

from veronica_core.runtime_policy import (
    RuntimePolicy,
    PolicyContext,
    PolicyDecision,
    PolicyPipeline,
)
from veronica_core.budget import BudgetEnforcer
from veronica_core.agent_guard import AgentStepGuard
from veronica_core.retry import RetryContainer
from veronica_core.circuit_breaker import CircuitBreaker, CircuitState


# --- Protocol conformance ---


class TestRuntimePolicyProtocol:
    """All 4 primitives must satisfy RuntimePolicy via isinstance()."""

    def test_budget_enforcer_is_runtime_policy(self):
        assert isinstance(BudgetEnforcer(), RuntimePolicy)

    def test_agent_step_guard_is_runtime_policy(self):
        assert isinstance(AgentStepGuard(), RuntimePolicy)

    def test_retry_container_is_runtime_policy(self):
        assert isinstance(RetryContainer(), RuntimePolicy)

    def test_circuit_breaker_is_runtime_policy(self):
        assert isinstance(CircuitBreaker(), RuntimePolicy)


# --- PolicyContext / PolicyDecision ---


class TestPolicyContext:
    def test_default_values(self):
        ctx = PolicyContext()
        assert ctx.cost_usd == 0.0
        assert ctx.step_count == 0
        assert ctx.entity_id == ""
        assert ctx.chain_id == ""
        assert isinstance(ctx.timestamp, float)
        assert isinstance(ctx.metadata, dict)

    def test_custom_values(self):
        ctx = PolicyContext(
            cost_usd=1.50,
            step_count=3,
            entity_id="user-1",
            chain_id="chain-abc",
            metadata={"model": "gpt-4"},
        )
        assert ctx.cost_usd == 1.50
        assert ctx.step_count == 3
        assert ctx.entity_id == "user-1"
        assert ctx.chain_id == "chain-abc"
        assert ctx.metadata == {"model": "gpt-4"}


class TestPolicyDecision:
    def test_allowed(self):
        d = PolicyDecision(allowed=True, policy_type="test")
        assert d.allowed
        assert d.policy_type == "test"
        assert d.reason == ""
        assert d.partial_result is None

    def test_denied_with_reason(self):
        d = PolicyDecision(
            allowed=False,
            policy_type="budget",
            reason="over limit",
        )
        assert not d.allowed
        assert d.policy_type == "budget"
        assert d.reason == "over limit"

    def test_denied_with_partial_result(self):
        d = PolicyDecision(
            allowed=False,
            policy_type="step_limit",
            reason="max steps",
            partial_result={"output": "partial"},
        )
        assert d.partial_result == {"output": "partial"}


# --- BudgetEnforcer policy ---


class TestBudgetEnforcerPolicy:
    def test_check_within_budget(self):
        b = BudgetEnforcer(limit_usd=10.0)
        decision = b.check(PolicyContext(cost_usd=5.0))
        assert decision.allowed
        assert decision.policy_type == "budget"

    def test_check_exceeds_budget(self):
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(8.0)
        decision = b.check(PolicyContext(cost_usd=5.0))
        assert not decision.allowed
        assert "exceed" in decision.reason.lower()

    def test_check_exactly_at_limit(self):
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(5.0)
        decision = b.check(PolicyContext(cost_usd=5.0))
        assert decision.allowed  # 5 + 5 = 10, not > 10

    def test_check_does_not_record_spending(self):
        b = BudgetEnforcer(limit_usd=10.0)
        b.check(PolicyContext(cost_usd=5.0))
        assert b.spent_usd == 0.0

    def test_policy_type(self):
        assert BudgetEnforcer().policy_type == "budget"


# --- AgentStepGuard policy ---


class TestAgentStepGuardPolicy:
    def test_check_within_limit(self):
        g = AgentStepGuard(max_steps=10)
        decision = g.check(PolicyContext())
        assert decision.allowed
        assert decision.policy_type == "step_limit"

    def test_check_at_limit(self):
        g = AgentStepGuard(max_steps=3)
        for _ in range(3):
            g.step()
        decision = g.check(PolicyContext())
        assert not decision.allowed
        assert "step limit" in decision.reason.lower()

    def test_check_preserves_partial_result(self):
        g = AgentStepGuard(max_steps=2)
        g.step(result="first")
        g.step(result="second")
        decision = g.check(PolicyContext())
        assert not decision.allowed
        assert decision.partial_result == "second"

    def test_policy_type(self):
        assert AgentStepGuard().policy_type == "step_limit"


# --- RetryContainer policy ---


class TestRetryContainerPolicy:
    def test_check_fresh_container(self):
        r = RetryContainer(max_retries=3)
        decision = r.check(PolicyContext())
        assert decision.allowed
        assert decision.policy_type == "retry_budget"

    def test_check_after_successful_execute(self):
        r = RetryContainer(max_retries=3, backoff_base=0.0)
        r.execute(lambda: 42)
        decision = r.check(PolicyContext())
        assert decision.allowed

    def test_check_after_exhausted_retries(self):
        r = RetryContainer(max_retries=1, backoff_base=0.0)
        with pytest.raises(ZeroDivisionError):
            r.execute(lambda: 1 / 0)
        decision = r.check(PolicyContext())
        assert not decision.allowed
        assert "exhausted" in decision.reason.lower()

    def test_reset_clears_error_state(self):
        r = RetryContainer(max_retries=1, backoff_base=0.0)
        with pytest.raises(ZeroDivisionError):
            r.execute(lambda: 1 / 0)
        r.reset()
        assert r.total_retries == 0
        assert r.last_error is None
        decision = r.check(PolicyContext())
        assert decision.allowed

    def test_check_allowed_after_successful_recovery(self):
        """After a failed execute, a successful execute should clear error state."""
        r = RetryContainer(max_retries=1, backoff_base=0.0)
        with pytest.raises(ZeroDivisionError):
            r.execute(lambda: 1 / 0)
        assert not r.check(PolicyContext()).allowed  # In error state
        r.execute(lambda: 42)  # Successful recovery
        decision = r.check(PolicyContext())
        assert decision.allowed  # Error state cleared

    def test_policy_type(self):
        assert RetryContainer().policy_type == "retry_budget"


# --- CircuitBreaker ---


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED

    def test_allows_when_closed(self):
        cb = CircuitBreaker()
        decision = cb.check(PolicyContext())
        assert decision.allowed
        assert decision.policy_type == "circuit_breaker"

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        decision = cb.check(PolicyContext())
        assert not decision.allowed

    def test_stays_closed_below_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.check(PolicyContext()).allowed

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.001)
        cb.record_failure()
        # Access internal _state directly to avoid triggering the half-open
        # transition that .state property may apply if enough time has passed.
        assert cb._state == CircuitState.OPEN
        time.sleep(0.05)  # 50x buffer for CI stability
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.check(PolicyContext()).allowed

    def test_closes_on_success_from_half_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.001)
        cb.record_failure()
        time.sleep(0.05)
        _ = cb.state  # trigger half-open
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0  # Counter reset on close

    def test_reopens_on_failure_from_half_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.001)
        cb.record_failure()
        time.sleep(0.05)
        _ = cb.state  # trigger half-open
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.failure_count == 0

    def test_reset(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_to_dict(self):
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        d = cb.to_dict()
        assert d["state"] == "CLOSED"
        assert d["failure_threshold"] == 5
        assert d["recovery_timeout"] == 30.0

    def test_policy_type(self):
        assert CircuitBreaker().policy_type == "circuit_breaker"


# --- PolicyPipeline ---


class TestPolicyPipeline:
    def test_empty_pipeline_allows(self):
        pipeline = PolicyPipeline()
        decision = pipeline.evaluate(PolicyContext())
        assert decision.allowed
        assert decision.policy_type == "pipeline"

    def test_single_policy_allows(self):
        pipeline = PolicyPipeline([BudgetEnforcer(limit_usd=100.0)])
        decision = pipeline.evaluate(PolicyContext(cost_usd=10.0))
        assert decision.allowed

    def test_all_policies_pass(self):
        pipeline = PolicyPipeline([
            BudgetEnforcer(limit_usd=100.0),
            AgentStepGuard(max_steps=25),
            CircuitBreaker(failure_threshold=5),
        ])
        decision = pipeline.evaluate(PolicyContext(cost_usd=10.0))
        assert decision.allowed

    def test_first_denial_wins(self):
        budget = BudgetEnforcer(limit_usd=10.0)
        budget.spend(9.0)
        guard = AgentStepGuard(max_steps=25)

        pipeline = PolicyPipeline([budget, guard])
        decision = pipeline.evaluate(PolicyContext(cost_usd=5.0))
        assert not decision.allowed
        assert decision.policy_type == "budget"

    def test_second_policy_denies(self):
        budget = BudgetEnforcer(limit_usd=100.0)
        guard = AgentStepGuard(max_steps=2)
        guard.step()
        guard.step()

        pipeline = PolicyPipeline([budget, guard])
        decision = pipeline.evaluate(PolicyContext(cost_usd=1.0))
        assert not decision.allowed
        assert decision.policy_type == "step_limit"

    def test_short_circuit_no_side_effects(self):
        """First denial stops evaluation -- later policies not checked."""
        budget = BudgetEnforcer(limit_usd=0.0)  # Denies any nonzero cost
        cb = CircuitBreaker(failure_threshold=5)

        pipeline = PolicyPipeline([budget, cb])
        decision = pipeline.evaluate(PolicyContext(cost_usd=1.0))
        assert not decision.allowed
        assert decision.policy_type == "budget"

    def test_add_policy(self):
        pipeline = PolicyPipeline()
        pipeline.add(BudgetEnforcer())
        assert len(pipeline) == 1

    def test_len(self):
        pipeline = PolicyPipeline([
            BudgetEnforcer(),
            AgentStepGuard(),
            CircuitBreaker(),
        ])
        assert len(pipeline) == 3

    def test_policies_returns_copy(self):
        budget = BudgetEnforcer()
        pipeline = PolicyPipeline([budget])
        policies = pipeline.policies
        policies.append(AgentStepGuard())
        assert len(pipeline) == 1  # Original unchanged


# --- Backward compatibility ---


class TestBackwardCompatibility:
    """v0.1 APIs MUST keep working unchanged."""

    def test_budget_spend(self):
        b = BudgetEnforcer(limit_usd=10.0)
        assert b.spend(5.0) is True
        assert b.spent_usd == 5.0
        assert b.remaining_usd == 5.0
        assert b.spend(6.0) is False
        assert b.is_exceeded

    def test_budget_reset(self):
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(10.0)
        b.reset()
        assert b.spent_usd == 0.0
        assert b.call_count == 0

    def test_budget_utilization(self):
        b = BudgetEnforcer(limit_usd=100.0)
        b.spend(50.0)
        assert b.utilization == 0.5

    def test_budget_to_dict(self):
        b = BudgetEnforcer(limit_usd=10.0)
        b.spend(3.0)
        d = b.to_dict()
        assert d["limit_usd"] == 10.0
        assert d["spent_usd"] == 3.0
        assert d["call_count"] == 1

    def test_agent_step(self):
        g = AgentStepGuard(max_steps=3)
        assert g.step(result="a") is True
        assert g.step(result="b") is True
        assert g.step(result="c") is False
        assert g.last_result == "c"
        assert g.is_exceeded

    def test_agent_remaining_steps(self):
        g = AgentStepGuard(max_steps=5)
        g.step()
        assert g.remaining_steps == 4

    def test_agent_reset(self):
        g = AgentStepGuard(max_steps=3)
        g.step()
        g.reset()
        assert g.current_step == 0
        assert g.last_result is None

    def test_retry_execute_success(self):
        r = RetryContainer(max_retries=2, backoff_base=0.0)
        result = r.execute(lambda: 42)
        assert result == 42

    def test_retry_retries_on_failure(self):
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "ok"

        r = RetryContainer(max_retries=3, backoff_base=0.0)
        result = r.execute(flaky)
        assert result == "ok"
        assert call_count == 3

    def test_retry_exhausts_raises(self):
        r = RetryContainer(max_retries=1, backoff_base=0.0)
        with pytest.raises(ZeroDivisionError):
            r.execute(lambda: 1 / 0)
        assert r.attempt_count == 2  # 1 initial + 1 retry
        assert r.total_retries == 2  # Both failures counted

    def test_partial_result_buffer_unchanged(self):
        from veronica_core.partial import PartialResultBuffer

        buf = PartialResultBuffer()
        buf.append("hello ")
        buf.append("world")
        assert buf.get_partial() == "hello world"
        assert buf.chunk_count == 2
        assert buf.is_partial
        buf.mark_complete()
        assert buf.is_complete

    def test_imports_unchanged(self):
        """All v0.1 public imports still work."""
        from veronica_core import (
            BudgetEnforcer,
            AgentStepGuard,
            PartialResultBuffer,
            RetryContainer,
            VeronicaStateMachine,
            VeronicaState,
            VeronicaIntegration,
            VeronicaGuard,
            PermissiveGuard,
        )
        # Just verify they're importable
        assert BudgetEnforcer is not None
        assert AgentStepGuard is not None
        assert PartialResultBuffer is not None
        assert RetryContainer is not None

    def test_new_imports_available(self):
        """v0.2 new exports are accessible."""
        from veronica_core import (
            RuntimePolicy,
            PolicyContext,
            PolicyDecision,
            PolicyPipeline,
            CircuitBreaker,
            CircuitState,
        )
        assert RuntimePolicy is not None
        assert PolicyPipeline is not None
        assert CircuitBreaker is not None
