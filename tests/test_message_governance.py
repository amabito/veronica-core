"""Tests for message governance hooks and bridge policy."""
from __future__ import annotations

import logging

import pytest

from veronica_core.memory.message_governance import (
    DefaultMessageGovernanceHook,
    DenyOversizedMessageHook,
    MessageBridgeHook,
    MessageGovernanceHook,
)
from veronica_core.memory.types import (
    BridgePolicy,
    GovernanceVerdict,
    MemoryProvenance,
    MessageContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(
    *,
    content_size_bytes: int = 100,
    trust_level: str = "trusted",
    provenance: MemoryProvenance = MemoryProvenance.UNVERIFIED,
    message_type: str = "agent_to_agent",
) -> MessageContext:
    return MessageContext(
        sender_id="agent-a",
        recipient_id="agent-b",
        message_type=message_type,
        content_size_bytes=content_size_bytes,
        trust_level=trust_level,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# DefaultMessageGovernanceHook
# ---------------------------------------------------------------------------

class TestDefaultMessageGovernanceHook:
    def test_allows_all_messages(self) -> None:
        hook = DefaultMessageGovernanceHook()
        decision = hook.before_message(_ctx())
        assert decision.verdict == GovernanceVerdict.ALLOW
        assert decision.allowed is True

    def test_allows_zero_size_message(self) -> None:
        hook = DefaultMessageGovernanceHook()
        decision = hook.before_message(_ctx(content_size_bytes=0))
        assert decision.verdict == GovernanceVerdict.ALLOW

    def test_policy_id_is_message_default(self) -> None:
        hook = DefaultMessageGovernanceHook()
        decision = hook.before_message(_ctx())
        assert decision.policy_id == "message_default"

    def test_after_message_no_error_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        hook = DefaultMessageGovernanceHook()
        ctx = _ctx()
        dec = hook.before_message(ctx)
        with caplog.at_level(logging.WARNING):
            hook.after_message(ctx, dec, result="ok", error=None)
        assert caplog.text == ""

    def test_after_message_logs_error_no_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        hook = DefaultMessageGovernanceHook()
        ctx = _ctx()
        dec = hook.before_message(ctx)
        err = RuntimeError("boom")
        with caplog.at_level(logging.WARNING):
            hook.after_message(ctx, dec, error=err)
        assert "boom" in caplog.text

    def test_after_message_does_not_raise(self) -> None:
        hook = DefaultMessageGovernanceHook()
        ctx = _ctx()
        dec = hook.before_message(ctx)
        # Must not raise even with an error
        hook.after_message(ctx, dec, error=ValueError("silent"))


# ---------------------------------------------------------------------------
# DenyOversizedMessageHook
# ---------------------------------------------------------------------------

class TestDenyOversizedMessageHook:
    def test_small_message_allows(self) -> None:
        hook = DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.8)
        decision = hook.before_message(_ctx(content_size_bytes=100))
        assert decision.verdict == GovernanceVerdict.ALLOW

    def test_large_message_denies(self) -> None:
        hook = DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.8)
        decision = hook.before_message(_ctx(content_size_bytes=1001))
        assert decision.verdict == GovernanceVerdict.DENY
        assert decision.denied is True

    def test_near_limit_degrades(self) -> None:
        hook = DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.8)
        # degrade_at = 800; 900 > 800 and <= 1000
        decision = hook.before_message(_ctx(content_size_bytes=900))
        assert decision.verdict == GovernanceVerdict.DEGRADE

    def test_degrade_has_directive_with_summary_required(self) -> None:
        hook = DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.8)
        decision = hook.before_message(_ctx(content_size_bytes=900))
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.summary_required is True
        assert decision.degrade_directive.mode == "compact"

    def test_degrade_directive_max_content_size(self) -> None:
        hook = DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.8)
        decision = hook.before_message(_ctx(content_size_bytes=900))
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.max_content_size_bytes == 800

    def test_degrade_has_threat_context(self) -> None:
        hook = DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.8)
        decision = hook.before_message(_ctx(content_size_bytes=900))
        assert decision.threat_context is not None
        assert decision.threat_context.compactness_enforced is True
        assert decision.threat_context.mitigation_applied == "degrade"

    def test_deny_has_threat_context(self) -> None:
        hook = DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.8)
        decision = hook.before_message(_ctx(content_size_bytes=9999))
        assert decision.threat_context is not None
        assert decision.threat_context.mitigation_applied == "deny"

    def test_deny_policy_id(self) -> None:
        hook = DenyOversizedMessageHook(max_bytes=1000)
        decision = hook.before_message(_ctx(content_size_bytes=1001))
        assert decision.policy_id == "message_size"

    def test_zero_max_bytes_raises(self) -> None:
        with pytest.raises(ValueError, match="max_bytes must be > 0"):
            DenyOversizedMessageHook(max_bytes=0)

    def test_negative_max_bytes_raises(self) -> None:
        with pytest.raises(ValueError, match="max_bytes must be > 0"):
            DenyOversizedMessageHook(max_bytes=-1)

    def test_invalid_threshold_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="degrade_threshold must be in"):
            DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.0)

    def test_invalid_threshold_raises(self) -> None:
        with pytest.raises(ValueError, match="degrade_threshold must be in"):
            DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=1.5)

    def test_exact_limit_allows(self) -> None:
        # content_size_bytes == max_bytes should ALLOW (not exceed)
        hook = DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.8)
        decision = hook.before_message(_ctx(content_size_bytes=1000))
        assert decision.verdict == GovernanceVerdict.ALLOW

    def test_exact_degrade_threshold_degrades(self) -> None:
        # degrade_at = int(1000 * 0.8) = 800; size 801 > 800 -> DEGRADE
        hook = DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.8)
        decision = hook.before_message(_ctx(content_size_bytes=801))
        assert decision.verdict == GovernanceVerdict.DEGRADE

    def test_exactly_at_degrade_threshold_allows(self) -> None:
        # size == degrade_at (800) should ALLOW (not strictly greater than)
        hook = DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.8)
        decision = hook.before_message(_ctx(content_size_bytes=800))
        assert decision.verdict == GovernanceVerdict.ALLOW

    def test_threat_source_trust_propagated(self) -> None:
        hook = DenyOversizedMessageHook(max_bytes=100)
        decision = hook.before_message(_ctx(content_size_bytes=200, trust_level="low"))
        assert decision.threat_context is not None
        assert decision.threat_context.source_trust == "low"

    def test_after_message_no_raise(self) -> None:
        hook = DenyOversizedMessageHook(max_bytes=1000)
        ctx = _ctx(content_size_bytes=100)
        dec = hook.before_message(ctx)
        hook.after_message(ctx, dec, error=RuntimeError("ignored"))


# ---------------------------------------------------------------------------
# MessageBridgeHook
# ---------------------------------------------------------------------------

class TestMessageBridgeHook:
    def test_no_archive_denies(self) -> None:
        hook = MessageBridgeHook(policy=BridgePolicy(allow_archive=False))
        decision = hook.before_message(_ctx(trust_level="trusted"))
        assert decision.verdict == GovernanceVerdict.DENY
        assert "archive not permitted" in decision.reason

    def test_default_policy_denies_because_allow_archive_false(self) -> None:
        # BridgePolicy default has allow_archive=False
        hook = MessageBridgeHook()
        decision = hook.before_message(_ctx(trust_level="trusted"))
        assert decision.verdict == GovernanceVerdict.DENY

    def test_archive_allowed_no_constraints_allows(self) -> None:
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=False,
            quarantine_untrusted=False,
        )
        hook = MessageBridgeHook(policy=policy)
        decision = hook.before_message(
            _ctx(trust_level="trusted", provenance=MemoryProvenance.UNVERIFIED)
        )
        assert decision.verdict == GovernanceVerdict.ALLOW

    def test_require_signature_unverified_denies(self) -> None:
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=True,
            quarantine_untrusted=False,
        )
        hook = MessageBridgeHook(policy=policy)
        decision = hook.before_message(
            _ctx(trust_level="trusted", provenance=MemoryProvenance.UNVERIFIED)
        )
        assert decision.verdict == GovernanceVerdict.DENY
        assert "signature required" in decision.reason

    def test_require_signature_verified_allows(self) -> None:
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=True,
            quarantine_untrusted=False,
        )
        hook = MessageBridgeHook(policy=policy)
        decision = hook.before_message(
            _ctx(trust_level="trusted", provenance=MemoryProvenance.VERIFIED)
        )
        assert decision.verdict == GovernanceVerdict.ALLOW

    def test_quarantine_untrusted_empty_trust(self) -> None:
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=False,
            quarantine_untrusted=True,
        )
        hook = MessageBridgeHook(policy=policy)
        decision = hook.before_message(_ctx(trust_level=""))
        assert decision.verdict == GovernanceVerdict.QUARANTINE

    def test_quarantine_untrusted_explicit(self) -> None:
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=False,
            quarantine_untrusted=True,
        )
        hook = MessageBridgeHook(policy=policy)
        decision = hook.before_message(_ctx(trust_level="untrusted"))
        assert decision.verdict == GovernanceVerdict.QUARANTINE
        assert "quarantined" in decision.reason

    def test_trusted_message_allows(self) -> None:
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=False,
            quarantine_untrusted=True,
        )
        hook = MessageBridgeHook(policy=policy)
        decision = hook.before_message(
            _ctx(trust_level="trusted", provenance=MemoryProvenance.UNVERIFIED)
        )
        assert decision.verdict == GovernanceVerdict.ALLOW

    def test_allowed_types_filter_denies_unknown(self) -> None:
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=False,
            quarantine_untrusted=False,
        )
        hook = MessageBridgeHook(
            policy=policy,
            allowed_message_types=frozenset({"tool_result"}),
        )
        decision = hook.before_message(_ctx(message_type="agent_to_agent", trust_level="trusted"))
        assert decision.verdict == GovernanceVerdict.DENY
        assert "not in allowed types" in decision.reason

    def test_allowed_types_filter_allows_known(self) -> None:
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=False,
            quarantine_untrusted=False,
        )
        hook = MessageBridgeHook(
            policy=policy,
            allowed_message_types=frozenset({"agent_to_agent", "tool_result"}),
        )
        decision = hook.before_message(_ctx(message_type="agent_to_agent", trust_level="trusted"))
        assert decision.verdict == GovernanceVerdict.ALLOW

    def test_allowed_types_none_skips_filter(self) -> None:
        # allowed_message_types=None means no filtering
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=False,
            quarantine_untrusted=False,
        )
        hook = MessageBridgeHook(policy=policy, allowed_message_types=None)
        decision = hook.before_message(_ctx(message_type="exotic_type", trust_level="trusted"))
        assert decision.verdict == GovernanceVerdict.ALLOW

    def test_quarantine_has_threat_context(self) -> None:
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=False,
            quarantine_untrusted=True,
        )
        hook = MessageBridgeHook(policy=policy)
        decision = hook.before_message(_ctx(trust_level="untrusted"))
        assert decision.threat_context is not None
        assert decision.threat_context.mitigation_applied == "quarantine"
        assert decision.threat_context.source_trust == "untrusted"

    def test_quarantine_empty_trust_source_shows_unknown(self) -> None:
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=False,
            quarantine_untrusted=True,
        )
        hook = MessageBridgeHook(policy=policy)
        decision = hook.before_message(_ctx(trust_level=""))
        assert decision.threat_context is not None
        assert decision.threat_context.source_trust == "unknown"

    def test_deny_signature_has_threat_context(self) -> None:
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=True,
            quarantine_untrusted=False,
        )
        hook = MessageBridgeHook(policy=policy)
        decision = hook.before_message(
            _ctx(trust_level="trusted", provenance=MemoryProvenance.UNVERIFIED)
        )
        assert decision.threat_context is not None
        assert "unsigned" in decision.threat_context.threat_hypothesis
        assert decision.threat_context.source_provenance == MemoryProvenance.UNVERIFIED.value

    def test_deny_no_archive_has_threat_context(self) -> None:
        hook = MessageBridgeHook(policy=BridgePolicy(allow_archive=False))
        decision = hook.before_message(_ctx())
        assert decision.threat_context is not None
        assert decision.threat_context.mitigation_applied == "deny"

    def test_policy_id_is_message_bridge(self) -> None:
        hook = MessageBridgeHook(policy=BridgePolicy(allow_archive=False))
        decision = hook.before_message(_ctx())
        assert decision.policy_id == "message_bridge"

    def test_after_message_no_raise(self) -> None:
        hook = MessageBridgeHook()
        ctx = _ctx()
        dec = hook.before_message(ctx)
        hook.after_message(ctx, dec, error=RuntimeError("ignored"))

    def test_signature_check_before_type_filter(self) -> None:
        # require_signature check should fire before allowed_types check
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=True,
            quarantine_untrusted=False,
        )
        hook = MessageBridgeHook(
            policy=policy,
            allowed_message_types=frozenset({"tool_result"}),
        )
        # unverified + wrong type: should get signature error first
        decision = hook.before_message(
            _ctx(
                trust_level="trusted",
                provenance=MemoryProvenance.UNVERIFIED,
                message_type="unknown_type",
            )
        )
        assert decision.verdict == GovernanceVerdict.DENY
        assert "signature required" in decision.reason

    def test_type_filter_before_quarantine(self) -> None:
        # allowed_types check fires before quarantine check
        policy = BridgePolicy(
            allow_archive=True,
            require_signature=False,
            quarantine_untrusted=True,
        )
        hook = MessageBridgeHook(
            policy=policy,
            allowed_message_types=frozenset({"tool_result"}),
        )
        # untrusted + wrong type: should get type deny first
        decision = hook.before_message(
            _ctx(trust_level="untrusted", message_type="unknown_type")
        )
        assert decision.verdict == GovernanceVerdict.DENY
        assert "not in allowed types" in decision.reason


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestMessageGovernanceHookProtocol:
    def test_default_hook_satisfies_protocol(self) -> None:
        assert isinstance(DefaultMessageGovernanceHook(), MessageGovernanceHook)

    def test_oversized_hook_satisfies_protocol(self) -> None:
        assert isinstance(DenyOversizedMessageHook(), MessageGovernanceHook)

    def test_bridge_hook_satisfies_protocol(self) -> None:
        assert isinstance(MessageBridgeHook(), MessageGovernanceHook)
