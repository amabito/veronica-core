"""Tests for veronica_core.adapters.ag2_capability.CircuitBreakerCapability.

Uses a minimal stub agent -- ag2 is not required.
"""
from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import pytest

from veronica_core.adapters.ag2_capability import CircuitBreakerCapability
from veronica_core.backends import MemoryBackend
from veronica_core.circuit_breaker import CircuitState
from veronica_core.integration import VeronicaIntegration
from veronica_core.shield.token_budget import TokenBudgetHook
from veronica_core.state import VeronicaState


# ---------------------------------------------------------------------------
# Stub agent
# ---------------------------------------------------------------------------


class StubAgent:
    """Minimal stand-in for ag2.ConversableAgent."""

    def __init__(self, name: str, fail_after: Optional[int] = None) -> None:
        self.name = name
        self._fail_after = fail_after
        self._call_count = 0

    def generate_reply(
        self,
        messages: Optional[list] = None,
        sender: Optional[object] = None,
    ) -> Optional[str]:
        self._call_count += 1
        if self._fail_after is not None and self._call_count > self._fail_after:
            return None
        return f"[{self.name}] reply #{self._call_count}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_veronica() -> VeronicaIntegration:
    return VeronicaIntegration(
        cooldown_fails=5,
        cooldown_seconds=60,
        backend=MemoryBackend(),
    )


# ---------------------------------------------------------------------------
# add_to_agent: basic behaviour
# ---------------------------------------------------------------------------


class TestAddToAgent:
    def test_returns_circuit_breaker(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=3)
        agent = StubAgent("a")
        breaker = cap.add_to_agent(agent)
        from veronica_core.circuit_breaker import CircuitBreaker
        assert isinstance(breaker, CircuitBreaker)

    def test_circuit_starts_closed(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=3)
        agent = StubAgent("a")
        cap.add_to_agent(agent)
        assert cap.get_breaker("a").state == CircuitState.CLOSED

    def test_healthy_reply_passes_through(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=3)
        agent = StubAgent("a")
        cap.add_to_agent(agent)
        reply = agent.generate_reply([])
        assert reply == "[a] reply #1"
        assert cap.get_breaker("a").state == CircuitState.CLOSED

    def test_original_call_count_increments(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=3)
        agent = StubAgent("a")
        cap.add_to_agent(agent)
        agent.generate_reply([])
        agent.generate_reply([])
        assert agent._call_count == 2

    def test_idempotent_second_call_is_noop(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=3)
        agent = StubAgent("a")
        breaker_first = cap.add_to_agent(agent)
        breaker_second = cap.add_to_agent(agent)
        assert breaker_first is breaker_second
        # Underlying function should not be double-wrapped
        agent.generate_reply([])
        assert agent._call_count == 1


# ---------------------------------------------------------------------------
# Circuit breaker state transitions
# ---------------------------------------------------------------------------


class TestCircuitTransitions:
    def test_failure_increments_count(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=3)
        agent = StubAgent("a", fail_after=0)
        cap.add_to_agent(agent)
        agent.generate_reply([])
        assert cap.get_breaker("a").failure_count == 1

    def test_success_resets_failure_count(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=3)
        agent = StubAgent("a", fail_after=1)
        cap.add_to_agent(agent)
        agent.generate_reply([])  # success -> fail_count=0
        agent.generate_reply([])  # fail    -> fail_count=1
        assert cap.get_breaker("a").failure_count == 1

    def test_circuit_opens_after_threshold(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=3)
        agent = StubAgent("a", fail_after=0)
        cap.add_to_agent(agent)
        for _ in range(3):
            agent.generate_reply([])
        assert cap.get_breaker("a").state == CircuitState.OPEN

    def test_open_circuit_blocks_without_calling_original(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=2)
        agent = StubAgent("a", fail_after=0)
        cap.add_to_agent(agent)
        # Trip the circuit
        agent.generate_reply([])
        agent.generate_reply([])
        assert cap.get_breaker("a").state == CircuitState.OPEN
        call_count_before = agent._call_count
        # Further calls must be blocked
        reply = agent.generate_reply([])
        assert reply is None
        assert agent._call_count == call_count_before  # original NOT called


# ---------------------------------------------------------------------------
# Per-agent independence
# ---------------------------------------------------------------------------


class TestPerAgentIndependence:
    def test_healthy_agent_unaffected_by_broken_agent(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=2)
        healthy = StubAgent("healthy")
        broken = StubAgent("broken", fail_after=0)
        cap.add_to_agent(healthy)
        cap.add_to_agent(broken)

        # Trip broken agent
        broken.generate_reply([])
        broken.generate_reply([])
        assert cap.get_breaker("broken").state == CircuitState.OPEN

        # Healthy agent must still work
        reply = healthy.generate_reply([])
        assert reply is not None
        assert cap.get_breaker("healthy").state == CircuitState.CLOSED

    def test_two_agents_have_independent_breakers(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=3)
        a = StubAgent("a")
        b = StubAgent("b")
        cap.add_to_agent(a)
        cap.add_to_agent(b)
        assert cap.get_breaker("a") is not cap.get_breaker("b")

    def test_breakers_property_contains_all_agents(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=3)
        cap.add_to_agent(StubAgent("x"))
        cap.add_to_agent(StubAgent("y"))
        assert set(cap.breakers.keys()) == {"x", "y"}


# ---------------------------------------------------------------------------
# SAFE_MODE integration
# ---------------------------------------------------------------------------


class TestSafeMode:
    def test_safe_mode_blocks_all_agents(self) -> None:
        veronica = _make_veronica()
        cap = CircuitBreakerCapability(failure_threshold=5, veronica=veronica)
        a = StubAgent("a")
        b = StubAgent("b")
        cap.add_to_agent(a)
        cap.add_to_agent(b)

        veronica.state.transition(VeronicaState.SAFE_MODE, reason="test")

        assert a.generate_reply([]) is None
        assert b.generate_reply([]) is None
        # Original must not have been called
        assert a._call_count == 0
        assert b._call_count == 0

    def test_safe_mode_cleared_unblocks_agents(self) -> None:
        veronica = _make_veronica()
        cap = CircuitBreakerCapability(failure_threshold=5, veronica=veronica)
        agent = StubAgent("a")
        cap.add_to_agent(agent)

        veronica.state.transition(VeronicaState.SAFE_MODE, reason="test")
        assert agent.generate_reply([]) is None

        veronica.state.transition(VeronicaState.IDLE, reason="resolved")
        veronica.state.transition(VeronicaState.SCREENING, reason="resuming")

        reply = agent.generate_reply([])
        assert reply is not None

    def test_safe_mode_takes_priority_over_closed_circuit(self) -> None:
        veronica = _make_veronica()
        cap = CircuitBreakerCapability(failure_threshold=5, veronica=veronica)
        agent = StubAgent("a")
        cap.add_to_agent(agent)
        # Circuit is CLOSED, but SAFE_MODE blocks
        veronica.state.transition(VeronicaState.SAFE_MODE, reason="test")
        assert cap.get_breaker("a").state == CircuitState.CLOSED
        assert agent.generate_reply([]) is None

    def test_no_veronica_does_not_check_safe_mode(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=3)  # no veronica
        agent = StubAgent("a")
        cap.add_to_agent(agent)
        reply = agent.generate_reply([])
        assert reply is not None


# ---------------------------------------------------------------------------
# get_breaker
# ---------------------------------------------------------------------------


class TestGetBreaker:
    def test_returns_none_for_unknown_agent(self) -> None:
        cap = CircuitBreakerCapability()
        assert cap.get_breaker("nonexistent") is None

    def test_returns_breaker_after_add(self) -> None:
        cap = CircuitBreakerCapability()
        agent = StubAgent("a")
        cap.add_to_agent(agent)
        assert cap.get_breaker("a") is not None

    def test_breaker_failure_threshold_matches_config(self) -> None:
        cap = CircuitBreakerCapability(failure_threshold=7, recovery_timeout=120)
        agent = StubAgent("a")
        cap.add_to_agent(agent)
        breaker = cap.get_breaker("a")
        assert breaker.failure_threshold == 7
        assert breaker.recovery_timeout == 120


# ---------------------------------------------------------------------------
# TokenBudgetHook integration
# ---------------------------------------------------------------------------


class TestTokenBudget:
    def test_token_budget_halt_blocks_call(self) -> None:
        """When token budget is exhausted (max=0), the call must be blocked."""
        hook = TokenBudgetHook(max_output_tokens=0)
        cap = CircuitBreakerCapability(failure_threshold=3, token_budget_hook=hook)
        agent = StubAgent("a")
        cap.add_to_agent(agent)

        reply = agent.generate_reply([])

        assert reply is None
        # Original must not have been called
        assert agent._call_count == 0

    def test_token_budget_records_usage(self) -> None:
        """After a successful reply, record_usage() must have been called."""
        hook = TokenBudgetHook(max_output_tokens=10000)
        cap = CircuitBreakerCapability(failure_threshold=3, token_budget_hook=hook)
        agent = StubAgent("a")
        cap.add_to_agent(agent)

        assert hook.output_total == 0
        reply = agent.generate_reply([])
        assert reply is not None
        # record_usage should have added tokens (word count of reply string)
        assert hook.output_total > 0


# ---------------------------------------------------------------------------
# kwargs forwarding
# ---------------------------------------------------------------------------


class TestKwargsForwarding:
    def test_extra_kwargs_forwarded_to_original(self) -> None:
        """generate_reply kwargs must reach the original method."""
        received: list = []

        class KwargsAgent:
            name = "kw"

            def generate_reply(self, messages=None, sender=None, **kwargs):
                received.append(kwargs)
                return "ok"

        cap = CircuitBreakerCapability(failure_threshold=3)
        agent = KwargsAgent()
        cap.add_to_agent(agent)
        agent.generate_reply([], extra="value")
        assert received == [{"extra": "value"}]


# ---------------------------------------------------------------------------
# remove_from_agent
# ---------------------------------------------------------------------------


class TestRemoveFromAgent:
    def test_remove_restores_original_generate_reply(self) -> None:
        """After remove_from_agent, agent.generate_reply must be the original method.

        Bound methods are not identity-comparable (new object each access), so
        we verify via __func__ equality on the underlying function object.
        """
        agent = StubAgent("r")
        original_func = agent.generate_reply.__func__  # underlying function
        cap = CircuitBreakerCapability(failure_threshold=3)
        cap.add_to_agent(agent)
        # After wrapping, the method should be the closure, not the original
        assert not hasattr(agent.generate_reply, "__func__") or \
            agent.generate_reply.__func__ is not original_func
        cap.remove_from_agent(agent)
        # After removal, the underlying function must be the original again
        assert agent.generate_reply.__func__ is original_func

    def test_remove_clears_breaker(self) -> None:
        """After remove_from_agent, get_breaker must return None."""
        agent = StubAgent("r3")
        cap = CircuitBreakerCapability(failure_threshold=3)
        cap.add_to_agent(agent)
        assert cap.get_breaker("r3") is not None
        cap.remove_from_agent(agent)
        assert cap.get_breaker("r3") is None

    def test_remove_clears_breakers_property(self) -> None:
        """breakers property must no longer contain removed agent."""
        a = StubAgent("ra")
        b = StubAgent("rb")
        cap = CircuitBreakerCapability(failure_threshold=3)
        cap.add_to_agent(a)
        cap.add_to_agent(b)
        cap.remove_from_agent(a)
        assert "ra" not in cap.breakers
        assert "rb" in cap.breakers

    def test_remove_unregistered_agent_is_noop(self) -> None:
        """remove_from_agent on an unregistered agent must not raise."""
        agent = StubAgent("not_registered")
        cap = CircuitBreakerCapability(failure_threshold=3)
        # Should not raise -- just warn
        cap.remove_from_agent(agent)

    def test_remove_allows_readd(self) -> None:
        """After remove, add_to_agent must work again on the same agent."""
        agent = StubAgent("rr")
        cap = CircuitBreakerCapability(failure_threshold=3)
        cap.add_to_agent(agent)
        cap.remove_from_agent(agent)
        # Re-add must succeed (not treated as duplicate)
        breaker = cap.add_to_agent(agent)
        assert breaker is not None
        assert cap.get_breaker("rr") is breaker
