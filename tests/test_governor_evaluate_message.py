"""Tests for MemoryGovernor.evaluate_message() -- message hook callpath.

Each test targets a distinct code branch in governor.py L193-303.
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.message_governance import (
    DefaultMessageGovernanceHook,
    DenyOversizedMessageHook,
)
from veronica_core.memory.types import (
    DegradeDirective,
    GovernanceVerdict,
    MemoryGovernanceDecision,
    MessageContext,
    ThreatContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(**overrides: Any) -> MessageContext:
    defaults = {
        "sender_id": "agent_a",
        "recipient_id": "agent_b",
        "message_type": "agent_to_agent",
        "content_size_bytes": 100,
        "trust_level": "trusted",
    }
    defaults.update(overrides)
    return MessageContext(**defaults)


class _FixedVerdictHook:
    """Hook that returns a fixed verdict from before_message."""

    def __init__(
        self,
        verdict: GovernanceVerdict = GovernanceVerdict.ALLOW,
        reason: str = "test",
        policy_id: str = "fixed",
        directive: DegradeDirective | None = None,
        threat: ThreatContext | None = None,
    ) -> None:
        self._decision = MemoryGovernanceDecision(
            verdict=verdict,
            reason=reason,
            policy_id=policy_id,
            degrade_directive=directive,
            threat_context=threat,
        )
        self.after_calls: list[MemoryGovernanceDecision] = []

    def before_message(self, context: MessageContext) -> MemoryGovernanceDecision:
        return self._decision

    def after_message(
        self,
        context: MessageContext,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        self.after_calls.append(decision)


class _RaisingHook:
    """Hook that raises in before_message."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def before_message(self, context: MessageContext) -> MemoryGovernanceDecision:
        raise self._exc

    def after_message(self, context: MessageContext, decision: MemoryGovernanceDecision, **kw: Any) -> None:
        pass


class _NoneHook:
    """Hook that returns None from before_message (protocol violation)."""

    def before_message(self, context: MessageContext) -> MemoryGovernanceDecision:
        return None  # type: ignore[return-value]

    def after_message(self, context: MessageContext, decision: MemoryGovernanceDecision, **kw: Any) -> None:
        pass


class _AfterRaisingHook(_FixedVerdictHook):
    """Hook that raises in after_message."""

    def after_message(
        self,
        context: MessageContext,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        raise RuntimeError("after_message boom")


# ---------------------------------------------------------------------------
# Branch tests
# ---------------------------------------------------------------------------


class TestEvaluateMessageBranches:
    """One test per distinct code branch in evaluate_message()."""

    # Branch 1: No hooks, fail-closed -> DENY
    def test_no_hooks_fail_closed_returns_deny(self) -> None:
        gov = MemoryGovernor(fail_closed=True)
        result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.DENY
        assert "fail-closed" in result.reason

    # Branch 2: No hooks, fail-open -> ALLOW
    def test_no_hooks_fail_open_returns_allow(self) -> None:
        gov = MemoryGovernor(fail_closed=False)
        result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.ALLOW
        assert "fail-open" in result.reason

    # Branch 3: Hook raises -> DENY with error reason
    def test_hook_raises_returns_deny(self) -> None:
        gov = MemoryGovernor()
        gov.add_message_hook(_RaisingHook(ValueError("bad input")))
        result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.DENY
        assert "hook error" in result.reason
        assert "ValueError" not in result.reason  # Rule 5: no exc type leak

    # Branch 4: Hook returns None -> TypeError -> DENY
    def test_hook_returns_none_deny(self) -> None:
        gov = MemoryGovernor()
        gov.add_message_hook(_NoneHook())
        result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.DENY
        assert "hook error" in result.reason
        assert "TypeError" not in result.reason  # Rule 5: no exc type leak

    # Branch 5: Hook returns DENY -> immediate break
    def test_deny_hook_short_circuits(self) -> None:
        gov = MemoryGovernor()
        deny_hook = _FixedVerdictHook(GovernanceVerdict.DENY, reason="blocked")
        allow_hook = _FixedVerdictHook(GovernanceVerdict.ALLOW)
        gov.add_message_hook(deny_hook)
        gov.add_message_hook(allow_hook)

        result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.DENY
        assert result.reason == "blocked"

    # Branch 6: Unknown verdict -> DENY
    def test_unknown_verdict_returns_deny(self) -> None:
        gov = MemoryGovernor()

        # Inject a hook that returns a verdict not in _VERDICT_RANK
        fake_verdict = MagicMock()
        fake_verdict.__class__ = GovernanceVerdict
        hook = _FixedVerdictHook()
        # Patch the decision to have a non-standard verdict
        hook._decision = MemoryGovernanceDecision(
            verdict=fake_verdict,
            reason="alien",
            policy_id="alien_hook",
        )
        gov.add_message_hook(hook)
        result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.DENY
        assert "unknown verdict" in result.reason

    # Branch 7: Single ALLOW hook -> ALLOW
    def test_single_allow_hook(self) -> None:
        gov = MemoryGovernor()
        gov.add_message_hook(DefaultMessageGovernanceHook())
        result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.ALLOW

    # Branch 8: DEGRADE with directive
    def test_degrade_hook_returns_directive(self) -> None:
        directive = DegradeDirective(mode="compact", summary_required=True)
        gov = MemoryGovernor()
        gov.add_message_hook(_FixedVerdictHook(
            GovernanceVerdict.DEGRADE,
            reason="near limit",
            directive=directive,
        ))
        result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.DEGRADE
        assert result.degrade_directive is not None
        assert result.degrade_directive.summary_required is True

    # Branch 9: Severity accumulation -- ALLOW + DEGRADE -> DEGRADE wins
    def test_allow_then_degrade_accumulates_to_degrade(self) -> None:
        gov = MemoryGovernor()
        gov.add_message_hook(_FixedVerdictHook(GovernanceVerdict.ALLOW))
        gov.add_message_hook(_FixedVerdictHook(
            GovernanceVerdict.DEGRADE,
            reason="size warning",
            directive=DegradeDirective(mode="compact"),
        ))
        result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.DEGRADE
        assert result.reason == "size warning"

    # Branch 10: after_message called on all hooks (even after DENY)
    def test_after_message_called_on_all_hooks_after_deny(self) -> None:
        gov = MemoryGovernor()
        hook_a = _FixedVerdictHook(GovernanceVerdict.DENY, reason="nope")
        hook_b = _FixedVerdictHook(GovernanceVerdict.ALLOW)
        gov.add_message_hook(hook_a)
        gov.add_message_hook(hook_b)

        gov.evaluate_message(_ctx())
        # Both hooks must get after_message called
        assert len(hook_a.after_calls) == 1
        assert len(hook_b.after_calls) == 1
        # Both receive the DENY decision
        assert hook_a.after_calls[0].verdict is GovernanceVerdict.DENY
        assert hook_b.after_calls[0].verdict is GovernanceVerdict.DENY

    # Branch 11: after_message raises -> logged, not propagated
    def test_after_message_exception_logged_not_propagated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gov = MemoryGovernor()
        gov.add_message_hook(_AfterRaisingHook())

        with caplog.at_level(logging.ERROR):
            result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.ALLOW
        assert "after_message" in caplog.text

    # Branch 12: ALLOW -> threat_context is None
    def test_allow_verdict_strips_threat_context(self) -> None:
        gov = MemoryGovernor()
        gov.add_message_hook(_FixedVerdictHook(
            GovernanceVerdict.ALLOW,
            threat=ThreatContext(threat_hypothesis="test"),
        ))
        result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.ALLOW
        assert result.threat_context is None


# ---------------------------------------------------------------------------
# Directive merge across multiple DEGRADE hooks
# ---------------------------------------------------------------------------


class TestDirectiveMerge:
    """Directive merge uses stricter-wins semantics across hooks."""

    def test_two_degrade_hooks_merge_directives(self) -> None:
        gov = MemoryGovernor()
        gov.add_message_hook(_FixedVerdictHook(
            GovernanceVerdict.DEGRADE,
            reason="hook1",
            directive=DegradeDirective(
                mode="compact",
                summary_required=False,
                max_content_size_bytes=5000,
            ),
        ))
        gov.add_message_hook(_FixedVerdictHook(
            GovernanceVerdict.DEGRADE,
            reason="hook2",
            directive=DegradeDirective(
                mode="redact",
                summary_required=True,
                max_content_size_bytes=3000,
            ),
        ))
        result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.DEGRADE
        d = result.degrade_directive
        assert d is not None
        # bool: OR -> True wins
        assert d.summary_required is True
        # int: stricter (smaller non-zero) wins
        assert d.max_content_size_bytes == 3000
        # str: last non-empty wins
        assert d.mode == "redact"

    def test_degrade_without_directive_does_not_crash(self) -> None:
        gov = MemoryGovernor()
        gov.add_message_hook(_FixedVerdictHook(
            GovernanceVerdict.DEGRADE,
            reason="no directive",
            directive=None,
        ))
        result = gov.evaluate_message(_ctx())
        assert result.verdict is GovernanceVerdict.DEGRADE
        assert result.degrade_directive is None


# ---------------------------------------------------------------------------
# Integration with real hook classes
# ---------------------------------------------------------------------------


class TestEvaluateMessageIntegration:
    """Use real DenyOversizedMessageHook + DefaultMessageGovernanceHook."""

    def test_oversized_message_denied(self) -> None:
        gov = MemoryGovernor()
        gov.add_message_hook(DenyOversizedMessageHook(max_bytes=1000))
        ctx = _ctx(content_size_bytes=2000)
        result = gov.evaluate_message(ctx)
        assert result.verdict is GovernanceVerdict.DENY

    def test_near_limit_message_degraded(self) -> None:
        gov = MemoryGovernor()
        gov.add_message_hook(DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.8))
        ctx = _ctx(content_size_bytes=900)
        result = gov.evaluate_message(ctx)
        assert result.verdict is GovernanceVerdict.DEGRADE
        assert result.degrade_directive is not None
        assert result.degrade_directive.summary_required is True

    def test_small_message_allowed(self) -> None:
        gov = MemoryGovernor()
        gov.add_message_hook(DenyOversizedMessageHook(max_bytes=1000))
        ctx = _ctx(content_size_bytes=100)
        result = gov.evaluate_message(ctx)
        assert result.verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# Adversarial: concurrent access
# ---------------------------------------------------------------------------


class TestEvaluateMessageConcurrency:
    """Concurrent evaluate_message calls must not corrupt state."""

    def test_concurrent_evaluate_message_no_exception(self) -> None:
        gov = MemoryGovernor()
        gov.add_message_hook(DenyOversizedMessageHook(max_bytes=1000))

        errors: list[Exception] = []

        def call() -> None:
            try:
                for _ in range(50):
                    gov.evaluate_message(_ctx(content_size_bytes=500))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected exceptions: {errors}"


# ---------------------------------------------------------------------------
# Compound state: DENY + after_message raise -- Rule 6
# ---------------------------------------------------------------------------


class TestDenyPlusAfterMessageRaise:
    def test_deny_verdict_not_swallowed_when_after_message_raises(self) -> None:
        """DENY from first hook must be returned even when a second hook raises in after_message.

        Verifies compound state: a DENY verdict (from before_message) and an exception
        (from after_message on another hook) must both be handled without the DENY
        being swallowed by the exception.
        """

        class _DenyHook:
            def before_message(self, context: MessageContext) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason="compound-deny",
                    policy_id="deny-hook",
                )

            def after_message(
                self,
                context: MessageContext,
                decision: MemoryGovernanceDecision,
                **kw: Any,
            ) -> None:
                pass

        class _AfterRaisingSecondHook:
            """Hook that returns ALLOW from before_message but raises in after_message."""

            def before_message(self, context: MessageContext) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    reason="second-allow",
                    policy_id="second-hook",
                )

            def after_message(
                self,
                context: MessageContext,
                decision: MemoryGovernanceDecision,
                **kw: Any,
            ) -> None:
                raise RuntimeError("after_message explosion in second hook")

        gov = MemoryGovernor()
        gov.add_message_hook(_DenyHook())
        gov.add_message_hook(_AfterRaisingSecondHook())

        # Must not raise; after_message exceptions are always swallowed.
        result = gov.evaluate_message(_ctx())

        assert result.verdict is GovernanceVerdict.DENY, (
            "DENY from first hook must survive even when second hook's after_message raises"
        )
        assert result.reason == "compound-deny"
