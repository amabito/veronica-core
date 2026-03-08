"""Tests for memory governance hook implementations.

Covers: MemoryGovernanceHook protocol, DefaultMemoryGovernanceHook,
        DenyAllMemoryGovernanceHook.
"""

from __future__ import annotations

import pytest

from veronica_core.memory.hooks import (
    DefaultMemoryGovernanceHook,
    DenyAllMemoryGovernanceHook,
    MemoryGovernanceHook,
)
from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
)


def _make_op(action: MemoryAction = MemoryAction.READ) -> MemoryOperation:
    return MemoryOperation(action=action, resource_id="test-res", agent_id="test-agent")


class TestDefaultMemoryGovernanceHook:
    def test_default_hook_allows(self) -> None:
        """DefaultMemoryGovernanceHook.before_op() must return ALLOW verdict."""
        hook = DefaultMemoryGovernanceHook()
        op = _make_op(MemoryAction.WRITE)
        ctx = MemoryPolicyContext(operation=op)
        decision = hook.before_op(op, ctx)
        assert isinstance(decision, MemoryGovernanceDecision)
        assert decision.verdict is GovernanceVerdict.ALLOW
        assert decision.allowed is True

    def test_default_hook_allows_with_none_context(self) -> None:
        """DefaultMemoryGovernanceHook.before_op() must handle None context."""
        hook = DefaultMemoryGovernanceHook()
        op = _make_op()
        decision = hook.before_op(op, None)
        assert decision.allowed is True

    def test_default_hook_after_op_with_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """DefaultMemoryGovernanceHook.after_op() must log errors and not raise."""
        import logging
        hook = DefaultMemoryGovernanceHook()
        op = _make_op()
        decision = MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW)
        error = RuntimeError("storage backend failure")

        with caplog.at_level(logging.WARNING, logger="veronica_core.memory.hooks"):
            hook.after_op(op, decision, error=error)  # must not raise

        assert any("storage backend failure" in r.message for r in caplog.records)

    def test_default_hook_after_op_no_error(self) -> None:
        """DefaultMemoryGovernanceHook.after_op() must not raise when no error."""
        hook = DefaultMemoryGovernanceHook()
        op = _make_op()
        decision = MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW)
        hook.after_op(op, decision, result={"data": "ok"})  # must not raise


class TestDenyAllMemoryGovernanceHook:
    def test_deny_all_hook_denies(self) -> None:
        """DenyAllMemoryGovernanceHook.before_op() must return DENY verdict."""
        hook = DenyAllMemoryGovernanceHook()
        op = _make_op(MemoryAction.WRITE)
        ctx = MemoryPolicyContext(operation=op)
        decision = hook.before_op(op, ctx)
        assert isinstance(decision, MemoryGovernanceDecision)
        assert decision.verdict is GovernanceVerdict.DENY
        assert decision.denied is True
        assert decision.allowed is False

    def test_deny_all_hook_after_op_noop(self) -> None:
        """DenyAllMemoryGovernanceHook.after_op() must be a no-op and not raise."""
        hook = DenyAllMemoryGovernanceHook()
        op = _make_op()
        decision = MemoryGovernanceDecision(verdict=GovernanceVerdict.DENY)
        hook.after_op(op, decision, error=ValueError("ignored"))  # must not raise


class TestHookProtocol:
    def test_hook_protocol_isinstance(self) -> None:
        """DefaultMemoryGovernanceHook and DenyAllMemoryGovernanceHook must satisfy the protocol."""
        default_hook = DefaultMemoryGovernanceHook()
        deny_hook = DenyAllMemoryGovernanceHook()
        assert isinstance(default_hook, MemoryGovernanceHook)
        assert isinstance(deny_hook, MemoryGovernanceHook)

    def test_custom_hook_satisfies_protocol(self) -> None:
        """Any object with before_op and after_op satisfies MemoryGovernanceHook."""

        class MyHook:
            def before_op(self, operation: MemoryOperation, context: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW)

            def after_op(self, operation: MemoryOperation, decision: MemoryGovernanceDecision, result=None, error=None) -> None:
                pass

        assert isinstance(MyHook(), MemoryGovernanceHook)
