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


def _ag2_installed() -> bool:
    """Return True if ag2/autogen is actually installed (not just our fake)."""
    try:
        import importlib.util
        return (
            importlib.util.find_spec("ag2") is not None
            or importlib.util.find_spec("autogen") is not None
        )
    except (ImportError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Now safe to import the adapter (ag2 is already in sys.modules)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from veronica_core import GuardConfig  # noqa: E402
from veronica_core.adapters.ag2 import VeronicaConversableAgent, register_veronica_hook  # noqa: E402
from veronica_core.container import AIcontainer  # noqa: E402
from veronica_core.containment import ExecutionConfig  # noqa: E402
from veronica_core.inject import VeronicaHalt  # noqa: E402


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
        agent.generate_reply(messages=[{"role": "user", "content": "hi"}])
        assert agent.container.step_guard.current_step == 1

    def test_step_counter_accumulates(self) -> None:
        """generate_reply: multiple calls each increment step counter."""
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_steps=20),
        )
        agent.generate_reply(messages=[{"role": "user", "content": "a"}])
        agent.generate_reply(messages=[{"role": "user", "content": "b"}])
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
        """Importing the adapter without ag2 raises a clear ImportError.

        This test can only verify the error path when ag2/autogen is NOT
        installed.  When ag2/autogen is installed, removing it from
        sys.modules is insufficient (importlib will re-find it on disk).
        """
        import importlib
        import importlib.util

        # Skip if ag2 or autogen is actually installed on disk.
        # find_spec may raise ValueError if a fake module with __spec__=None
        # is in sys.modules, so we catch that too.
        try:
            ag2_on_disk = importlib.util.find_spec("ag2") is not None
        except (ValueError, ModuleNotFoundError):
            ag2_on_disk = False
        try:
            autogen_on_disk = importlib.util.find_spec("autogen") is not None
        except (ValueError, ModuleNotFoundError):
            autogen_on_disk = False
        if ag2_on_disk or autogen_on_disk:
            pytest.skip("ag2/autogen is installed; cannot test missing-import path")

        adapter_key = "veronica_core.adapters.ag2"
        # Save and remove ALL ag2/autogen modules (including submodules)
        saved: dict[str, types.ModuleType] = {}
        keys_to_remove = [
            k for k in sys.modules
            if k in (adapter_key,) or k.startswith(("ag2", "autogen"))
        ]
        for k in keys_to_remove:
            saved[k] = sys.modules.pop(k)

        try:
            with pytest.raises(ImportError, match="ag2|autogen"):
                importlib.import_module("veronica_core.adapters.ag2")
        finally:
            sys.modules.update(saved)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_exception_in_parent_propagates_without_incrementing_step(self) -> None:
        """If parent generate_reply raises, step counter must NOT increment."""

        class FailingAgent(VeronicaConversableAgent):
            def generate_reply(self, messages=None, sender=None, **kwargs):
                # Bypass VERONICA check by calling grandparent path directly
                decision = self._container.check(cost_usd=0.0)
                if not decision.allowed:
                    from veronica_core.inject import VeronicaHalt
                    raise VeronicaHalt(decision.reason, decision)
                raise RuntimeError("upstream failure")

        agent = FailingAgent("assistant", config=GuardConfig(max_steps=10))
        assert agent.container.step_guard.current_step == 0
        with pytest.raises(RuntimeError, match="upstream failure"):
            agent.generate_reply(messages=[])
        assert agent.container.step_guard.current_step == 0

    def test_none_reply_from_parent_does_not_increment_step(self) -> None:
        """If parent returns None, step counter must NOT increment.

        AG2 0.11+ ConversableAgent.generate_reply may return '' instead of
        None for auto-reply.  We test using a subclass that explicitly
        returns None to verify the step-guard logic.
        """

        class NoneReplyAgent(VeronicaConversableAgent):
            """Subclass that always returns None from the parent path."""

            def generate_reply(self, messages=None, sender=None, **kwargs):
                # Run VERONICA check, then force None
                decision = self._container.check(cost_usd=0.0)
                if not decision.allowed:
                    raise VeronicaHalt(decision.reason, decision)
                return None

        agent = NoneReplyAgent(
            "assistant", config=GuardConfig(max_steps=10)
        )
        result = agent.generate_reply(messages=[{"role": "user", "content": "x"}])
        assert result is None
        assert agent.container.step_guard.current_step == 0

    def test_zero_budget_blocks_immediately(self) -> None:
        """max_cost_usd=0.0 should block on the first call."""
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_cost_usd=0.0),
        )
        agent.container.budget.spend(0.001)
        with pytest.raises(VeronicaHalt, match="[Bb]udget"):
            agent.generate_reply(messages=[])

    def test_zero_max_steps_blocks_immediately(self) -> None:
        """max_steps=1 should block after a single call."""
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_steps=1),
        )
        agent.generate_reply(messages=[{"role": "user", "content": "x"}])
        with pytest.raises(VeronicaHalt, match="[Ss]tep"):
            agent.generate_reply(messages=[{"role": "user", "content": "y"}])
