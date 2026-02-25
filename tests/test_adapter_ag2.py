"""Tests for veronica_core.adapters.ag2 — AG2 ConversableAgent adapter.

Uses fake ag2 stubs injected into sys.modules so ag2 does not need to
be installed.
"""
from __future__ import annotations

import sys
import types

# ---------------------------------------------------------------------------
# Inject fake ag2 stubs BEFORE importing the adapter
# ---------------------------------------------------------------------------


def _build_fake_ag2() -> type:
    """Create minimal ag2 stubs and register in sys.modules.

    Returns the FakeConversableAgent class for use in test helpers.
    """
    ag2_mod = types.ModuleType("ag2")

    class FakeConversableAgent:
        """Minimal ConversableAgent stand-in."""

        def __init__(self, name: str = "agent", **kwargs) -> None:
            self.name = name
            self._reply_funcs: list = []

        def generate_reply(self, messages=None, sender=None, **kwargs):
            """Return a fixed reply."""
            return "Hello from stub"

        def register_reply(self, trigger, reply_func, position: int = 0) -> None:
            """Record registered reply functions."""
            self._reply_funcs.insert(position, (trigger, reply_func))

    ag2_mod.ConversableAgent = FakeConversableAgent
    sys.modules["ag2"] = ag2_mod
    return FakeConversableAgent


FakeConversableAgent = _build_fake_ag2()

# ---------------------------------------------------------------------------
# Now safe to import the adapter (ag2 is already in sys.modules)
# ---------------------------------------------------------------------------

import pytest

from veronica_core import GuardConfig
from veronica_core.adapters.ag2 import VeronicaConversableAgent, register_veronica_hook
from veronica_core.container import AIcontainer
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import VeronicaHalt


# ---------------------------------------------------------------------------
# Allow path
# ---------------------------------------------------------------------------


class TestAllowPath:
    def test_healthy_reply_passes(self) -> None:
        """generate_reply: no exception raised when policies allow."""
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_cost_usd=10.0, max_steps=10),
        )
        reply = agent.generate_reply(messages=[{"role": "user", "content": "Hi"}])
        assert reply is not None

    def test_step_counter_increments(self) -> None:
        """generate_reply: step_guard.current_step increments after each call."""
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_steps=10),
        )
        assert agent.container.step_guard.current_step == 0
        agent.generate_reply(messages=[])
        assert agent.container.step_guard.current_step == 1

    def test_step_counter_accumulates(self) -> None:
        """generate_reply: multiple calls each increment step counter."""
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_steps=20),
        )
        agent.generate_reply(messages=[])
        agent.generate_reply(messages=[])
        assert agent.container.step_guard.current_step == 2

    def test_container_property_returns_aicontainer(self) -> None:
        """container property returns the underlying AIcontainer."""
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_cost_usd=5.0),
        )
        assert isinstance(agent.container, AIcontainer)


# ---------------------------------------------------------------------------
# Deny path
# ---------------------------------------------------------------------------


class TestDenyPath:
    def test_budget_exhausted_raises_veronica_halt(self) -> None:
        """generate_reply: raises VeronicaHalt when budget is pre-exhausted."""
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_cost_usd=1.0),
        )
        agent.container.budget.spend(2.0)  # exhaust manually
        with pytest.raises(VeronicaHalt, match="[Bb]udget"):
            agent.generate_reply(messages=[])

    def test_step_limit_raises_veronica_halt(self) -> None:
        """generate_reply: raises VeronicaHalt when step limit is exhausted."""
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_steps=2),
        )
        # Exhaust steps manually
        agent.container.step_guard.step()
        agent.container.step_guard.step()
        with pytest.raises(VeronicaHalt, match="[Ss]tep"):
            agent.generate_reply(messages=[])

    def test_veronica_halt_carries_decision(self) -> None:
        """VeronicaHalt from deny path carries a PolicyDecision."""
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_cost_usd=0.0),
        )
        agent.container.budget.spend(1.0)
        with pytest.raises(VeronicaHalt) as exc_info:
            agent.generate_reply(messages=[])
        assert exc_info.value.decision is not None
        assert not exc_info.value.decision.allowed


# ---------------------------------------------------------------------------
# Config acceptance
# ---------------------------------------------------------------------------


class TestConfigAcceptance:
    def test_accepts_guard_config(self) -> None:
        """VeronicaConversableAgent accepts GuardConfig."""
        cfg = GuardConfig(max_cost_usd=5.0, max_steps=10, max_retries_total=3)
        agent = VeronicaConversableAgent("assistant", config=cfg)
        assert agent.container.budget.limit_usd == 5.0
        assert agent.container.step_guard.max_steps == 10

    def test_accepts_execution_config(self) -> None:
        """VeronicaConversableAgent accepts ExecutionConfig."""
        cfg = ExecutionConfig(max_cost_usd=3.0, max_steps=15, max_retries_total=5)
        agent = VeronicaConversableAgent("assistant", config=cfg)
        assert agent.container.budget.limit_usd == 3.0
        assert agent.container.step_guard.max_steps == 15


# ---------------------------------------------------------------------------
# Hook-based integration path
# ---------------------------------------------------------------------------


class TestHookPath:
    def test_register_veronica_hook_returns_container(self) -> None:
        """register_veronica_hook returns an AIcontainer."""
        agent = FakeConversableAgent("hook-agent")
        container = register_veronica_hook(agent, GuardConfig(max_cost_usd=5.0))
        assert isinstance(container, AIcontainer)

    def test_register_veronica_hook_registers_reply_func(self) -> None:
        """register_veronica_hook registers exactly one reply function."""
        agent = FakeConversableAgent("hook-agent")
        assert len(agent._reply_funcs) == 0
        register_veronica_hook(agent, GuardConfig(max_cost_usd=5.0))
        assert len(agent._reply_funcs) == 1

    def test_register_veronica_hook_blocks_on_policy_deny(self) -> None:
        """Registered reply function raises VeronicaHalt when budget exhausted."""
        agent = FakeConversableAgent("hook-agent")
        container = register_veronica_hook(agent, GuardConfig(max_cost_usd=1.0))
        container.budget.spend(2.0)  # exhaust manually

        # Invoke the registered reply function directly
        _trigger, reply_fn = agent._reply_funcs[0]
        with pytest.raises(VeronicaHalt, match="[Bb]udget"):
            reply_fn(agent, messages=[], sender=None, config=None)

    def test_register_veronica_hook_passes_when_healthy(self) -> None:
        """Registered reply function returns (False, None) when policies allow."""
        agent = FakeConversableAgent("hook-agent")
        register_veronica_hook(agent, GuardConfig(max_cost_usd=5.0, max_steps=10))

        _trigger, reply_fn = agent._reply_funcs[0]
        result = reply_fn(agent, messages=[], sender=None, config=None)
        assert result == (False, None)


# ---------------------------------------------------------------------------
# Import error when ag2 absent
# ---------------------------------------------------------------------------


class TestImportError:
    def test_raises_import_error_when_ag2_absent(self) -> None:
        """Importing the adapter without ag2 raises a clear ImportError."""
        import importlib

        adapter_key = "veronica_core.adapters.ag2"
        saved_adapter = sys.modules.pop(adapter_key, None)
        saved_ag2 = sys.modules.pop("ag2", None)

        try:
            with pytest.raises(ImportError, match="ag2"):
                importlib.import_module("veronica_core.adapters.ag2")
        finally:
            if saved_adapter is not None:
                sys.modules[adapter_key] = saved_adapter
            if saved_ag2 is not None:
                sys.modules["ag2"] = saved_ag2
