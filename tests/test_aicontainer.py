"""Unit tests for AIcontainer (veronica_core.container)."""
from __future__ import annotations

import pytest

from veronica_core.container import AIcontainer
from veronica_core import (
    AgentStepGuard,
    BudgetEnforcer,
    CircuitBreaker,
    PartialResultBuffer,
    RetryContainer,
)


class TestAIcontainerInstantiation:
    def test_empty_instantiation(self) -> None:
        container = AIcontainer()
        assert container.budget is None
        assert container.circuit_breaker is None
        assert container.retry is None
        assert container.step_guard is None
        assert container.partial_buffer is None

    def test_with_budget_and_circuit_breaker(self) -> None:
        budget = BudgetEnforcer(limit_usd=10.0)
        breaker = CircuitBreaker(failure_threshold=3)
        container = AIcontainer(budget=budget, circuit_breaker=breaker)
        assert container.budget is budget
        assert container.circuit_breaker is breaker

    def test_primitives_are_exposed_as_fields(self) -> None:
        """Primitives must be accessible as named fields without modification."""
        budget = BudgetEnforcer(limit_usd=50.0)
        retry = RetryContainer(max_retries=5)
        container = AIcontainer(budget=budget, retry=retry)
        assert container.budget.limit_usd == 50.0
        assert container.retry.max_retries == 5


class TestAIcontainerActivePolicies:
    def test_empty_container_has_no_active_policies(self) -> None:
        container = AIcontainer()
        assert container.active_policies == []

    def test_active_policies_reflect_provided_primitives(self) -> None:
        container = AIcontainer(
            budget=BudgetEnforcer(),
            circuit_breaker=CircuitBreaker(),
            retry=RetryContainer(),
            step_guard=AgentStepGuard(),
        )
        assert set(container.active_policies) == {
            "budget",
            "circuit_breaker",
            "retry_budget",
            "step_limit",
        }

    def test_partial_buffer_is_not_a_policy(self) -> None:
        """PartialResultBuffer does not implement RuntimePolicy; must not appear in active_policies."""
        container = AIcontainer(partial_buffer=PartialResultBuffer())
        assert container.active_policies == []


class TestAIcontainerCheck:
    def test_check_allows_within_budget(self) -> None:
        container = AIcontainer(budget=BudgetEnforcer(limit_usd=10.0))
        decision = container.check(cost_usd=1.0)
        assert decision.allowed is True

    def test_check_denies_when_budget_exceeded(self) -> None:
        container = AIcontainer(budget=BudgetEnforcer(limit_usd=1.0))
        decision = container.check(cost_usd=5.0)
        assert decision.allowed is False
        assert decision.policy_type == "budget"

    def test_check_on_empty_container_always_allows(self) -> None:
        container = AIcontainer()
        decision = container.check(cost_usd=99999.0)
        assert decision.allowed is True

    def test_check_passes_kwargs_to_pipeline(self) -> None:
        container = AIcontainer(step_guard=AgentStepGuard(max_steps=5))
        # step_count is in PolicyContext but AgentStepGuard uses its own counter
        decision = container.check(cost_usd=0.0, step_count=1)
        assert decision.allowed is True

    def test_check_first_denial_wins(self) -> None:
        """Budget denial must short-circuit and not evaluate remaining policies."""
        container = AIcontainer(
            budget=BudgetEnforcer(limit_usd=0.01),
            step_guard=AgentStepGuard(max_steps=100),
        )
        decision = container.check(cost_usd=1.0)
        assert decision.allowed is False
        assert decision.policy_type == "budget"


class TestAIcontainerReset:
    def test_reset_delegates_to_budget(self) -> None:
        budget = BudgetEnforcer(limit_usd=10.0)
        budget.spend(5.0)
        assert budget.spent_usd == 5.0

        container = AIcontainer(budget=budget)
        container.reset()

        assert budget.spent_usd == 0.0

    def test_reset_delegates_to_circuit_breaker(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.failure_count == 2

        container = AIcontainer(circuit_breaker=breaker)
        container.reset()

        assert breaker.failure_count == 0

    def test_reset_clears_partial_buffer(self) -> None:
        buf = PartialResultBuffer()
        buf.append("partial text")
        assert buf.chunk_count == 1

        container = AIcontainer(partial_buffer=buf)
        container.reset()

        assert buf.chunk_count == 0

    def test_reset_preserves_active_policies(self) -> None:
        """After reset, active_policies must remain the same set."""
        container = AIcontainer(
            budget=BudgetEnforcer(),
            step_guard=AgentStepGuard(),
        )
        policies_before = set(container.active_policies)
        container.reset()
        assert set(container.active_policies) == policies_before

    def test_check_allows_after_reset(self) -> None:
        """After reset, a previously-denied container must allow again."""
        budget = BudgetEnforcer(limit_usd=1.0)
        budget.spend(2.0)  # Exceed budget manually
        container = AIcontainer(budget=budget)

        # Should deny before reset (projected 0.0 + spent 2.0 = 2.0 > limit 1.0)
        decision_before = container.check(cost_usd=0.0)
        assert decision_before.allowed is False

        container.reset()
        decision_after = container.check(cost_usd=0.5)
        assert decision_after.allowed is True
