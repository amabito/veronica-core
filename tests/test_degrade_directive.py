"""Tests for DegradeDirective, CompactnessConstraints, new type extensions, and governor merging."""

from __future__ import annotations

import pytest

from veronica_core.memory.types import (
    BridgePolicy,
    CompactnessConstraints,
    DegradeDirective,
    ExecutionMode,
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
    MemoryProvenance,
    MemoryView,
    MessageContext,
    ThreatContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _op(action: MemoryAction = MemoryAction.READ) -> MemoryOperation:
    return MemoryOperation(action=action)


def _make_degrade_decision(
    directive: DegradeDirective | None = None,
    threat: ThreatContext | None = None,
    policy_id: str = "test",
) -> MemoryGovernanceDecision:
    return MemoryGovernanceDecision(
        verdict=GovernanceVerdict.DEGRADE,
        reason="degrade",
        policy_id=policy_id,
        degrade_directive=directive,
        threat_context=threat,
    )


# ---------------------------------------------------------------------------
# DegradeDirective
# ---------------------------------------------------------------------------


class TestDegradeDirective:
    def test_default_values(self) -> None:
        d = DegradeDirective()
        assert d.mode == ""
        assert d.max_packet_tokens == 0
        assert d.allowed_provenance == ()
        assert d.verified_only is False
        assert d.summary_required is False
        assert d.raw_replay_blocked is False
        assert d.namespace_downscoped_to == ""
        assert d.redacted_fields == ()
        assert d.max_content_size_bytes == 0

    def test_frozen_immutable(self) -> None:
        d = DegradeDirective(mode="compact")
        with pytest.raises(AttributeError):
            d.mode = "redact"  # type: ignore[misc]

    def test_with_all_fields(self) -> None:
        d = DegradeDirective(
            mode="compact",
            max_packet_tokens=500,
            allowed_provenance=("verified", "unverified"),
            verified_only=True,
            summary_required=True,
            raw_replay_blocked=True,
            namespace_downscoped_to="safe",
            redacted_fields=("secret",),
            max_content_size_bytes=1024,
        )
        assert d.mode == "compact"
        assert d.max_packet_tokens == 500
        assert set(d.allowed_provenance) == {"verified", "unverified"}
        assert d.verified_only is True
        assert d.summary_required is True
        assert d.raw_replay_blocked is True
        assert d.namespace_downscoped_to == "safe"
        assert d.redacted_fields == ("secret",)
        assert d.max_content_size_bytes == 1024

    def test_equality(self) -> None:
        d1 = DegradeDirective(mode="compact", verified_only=True)
        d2 = DegradeDirective(mode="compact", verified_only=True)
        assert d1 == d2

    def test_inequality_on_field(self) -> None:
        d1 = DegradeDirective(mode="compact")
        d2 = DegradeDirective(mode="redact")
        assert d1 != d2

    def test_tuple_fields_are_tuples(self) -> None:
        d = DegradeDirective(
            allowed_provenance=("verified",),
            redacted_fields=("a", "b"),
        )
        assert isinstance(d.allowed_provenance, tuple)
        assert isinstance(d.redacted_fields, tuple)


# ---------------------------------------------------------------------------
# MemoryView
# ---------------------------------------------------------------------------


class TestMemoryView:
    def test_all_values(self) -> None:
        assert len(MemoryView) == 7

    def test_is_string(self) -> None:
        assert MemoryView.AGENT_PRIVATE == "agent_private"
        assert MemoryView.LOCAL_WORKING == "local_working"
        assert MemoryView.TEAM_SHARED == "team_shared"
        assert MemoryView.SESSION_STATE == "session_state"
        assert MemoryView.VERIFIED_ARCHIVE == "verified_archive"
        assert MemoryView.PROVISIONAL_ARCHIVE == "provisional_archive"
        assert MemoryView.QUARANTINED == "quarantined"

    def test_str_enum(self) -> None:
        assert isinstance(MemoryView.LOCAL_WORKING, str)


# ---------------------------------------------------------------------------
# ExecutionMode
# ---------------------------------------------------------------------------


class TestExecutionMode:
    def test_all_values(self) -> None:
        assert len(ExecutionMode) == 5

    def test_live_default(self) -> None:
        assert ExecutionMode.LIVE == "live_execution"

    def test_all_members(self) -> None:
        assert ExecutionMode.REPLAY == "replay"
        assert ExecutionMode.SIMULATION == "simulation"
        assert ExecutionMode.CONSOLIDATION == "consolidation"
        assert ExecutionMode.AUDIT_REVIEW == "audit_review"

    def test_str_enum(self) -> None:
        assert isinstance(ExecutionMode.LIVE, str)


# ---------------------------------------------------------------------------
# CompactnessConstraints
# ---------------------------------------------------------------------------


class TestCompactnessConstraints:
    def test_default_values(self) -> None:
        c = CompactnessConstraints()
        assert c.max_packet_tokens == 0
        assert c.max_raw_replay_ratio == 1.0
        assert c.require_compaction_if_over_budget is False
        assert c.prefer_verified_summary is False
        assert c.max_attributes_per_packet == 0
        assert c.max_payload_bytes == 0

    def test_frozen(self) -> None:
        c = CompactnessConstraints(max_packet_tokens=100)
        with pytest.raises(AttributeError):
            c.max_packet_tokens = 200  # type: ignore[misc]

    def test_with_all_fields(self) -> None:
        c = CompactnessConstraints(
            max_packet_tokens=256,
            max_raw_replay_ratio=0.5,
            require_compaction_if_over_budget=True,
            prefer_verified_summary=True,
            max_attributes_per_packet=10,
            max_payload_bytes=4096,
        )
        assert c.max_packet_tokens == 256
        assert c.max_raw_replay_ratio == 0.5
        assert c.require_compaction_if_over_budget is True
        assert c.prefer_verified_summary is True
        assert c.max_attributes_per_packet == 10
        assert c.max_payload_bytes == 4096

    def test_equality(self) -> None:
        c1 = CompactnessConstraints(max_packet_tokens=100)
        c2 = CompactnessConstraints(max_packet_tokens=100)
        assert c1 == c2


# ---------------------------------------------------------------------------
# MessageContext
# ---------------------------------------------------------------------------


class TestMessageContext:
    def test_default_values(self) -> None:
        mc = MessageContext()
        assert mc.sender_id == ""
        assert mc.recipient_id == ""
        assert mc.message_type == ""
        assert mc.content_size_bytes == 0
        assert mc.trust_level == ""
        assert mc.provenance == MemoryProvenance.UNKNOWN
        assert mc.namespace == ""

    def test_negative_size_raises(self) -> None:
        with pytest.raises(ValueError, match="must be >= 0"):
            MessageContext(content_size_bytes=-1)

    def test_zero_size_valid(self) -> None:
        mc = MessageContext(content_size_bytes=0)
        assert mc.content_size_bytes == 0

    def test_metadata_frozen(self) -> None:
        mc = MessageContext(metadata={"key": "val"})
        with pytest.raises(TypeError):
            mc.metadata["new"] = "val"  # type: ignore[index]

    def test_metadata_preserved(self) -> None:
        mc = MessageContext(metadata={"k": "v"})
        assert mc.metadata["k"] == "v"

    def test_frozen_instance(self) -> None:
        mc = MessageContext(sender_id="a")
        with pytest.raises(AttributeError):
            mc.sender_id = "b"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BridgePolicy
# ---------------------------------------------------------------------------


class TestBridgePolicy:
    def test_default_values(self) -> None:
        bp = BridgePolicy()
        assert bp.allow_archive is False
        assert bp.require_signature is False
        assert bp.max_promotion_level == "provisional"
        assert bp.quarantine_untrusted is True
        assert bp.write_once_scratch is True

    def test_frozen(self) -> None:
        bp = BridgePolicy(allow_archive=True)
        with pytest.raises(AttributeError):
            bp.allow_archive = False  # type: ignore[misc]

    def test_custom_values(self) -> None:
        bp = BridgePolicy(
            allow_archive=True,
            require_signature=True,
            max_promotion_level="verified_archive",
            quarantine_untrusted=False,
            write_once_scratch=False,
        )
        assert bp.allow_archive is True
        assert bp.require_signature is True
        assert bp.max_promotion_level == "verified_archive"
        assert bp.quarantine_untrusted is False
        assert bp.write_once_scratch is False


# ---------------------------------------------------------------------------
# ThreatContext
# ---------------------------------------------------------------------------


class TestThreatContext:
    def test_default_values(self) -> None:
        tc = ThreatContext()
        assert tc.threat_hypothesis == ""
        assert tc.mitigation_applied == ""
        assert tc.degrade_reason == ""
        assert tc.degraded_fields == ()
        assert tc.effective_scope == ""
        assert tc.effective_view == ""
        assert tc.compactness_enforced is False
        assert tc.source_trust == ""
        assert tc.source_provenance == ""

    def test_frozen(self) -> None:
        tc = ThreatContext(threat_hypothesis="test")
        with pytest.raises(AttributeError):
            tc.threat_hypothesis = "modified"  # type: ignore[misc]

    def test_with_fields(self) -> None:
        tc = ThreatContext(
            threat_hypothesis="memory poisoning",
            mitigation_applied="degrade",
            source_trust="untrusted",
        )
        assert tc.threat_hypothesis == "memory poisoning"
        assert tc.mitigation_applied == "degrade"
        assert tc.source_trust == "untrusted"


# ---------------------------------------------------------------------------
# MemoryPolicyContext -- new v3.6.0 fields
# ---------------------------------------------------------------------------


class TestMemoryPolicyContextExtensions:
    def test_default_new_fields(self) -> None:
        op = _op()
        ctx = MemoryPolicyContext(operation=op)
        assert ctx.memory_view == MemoryView.LOCAL_WORKING
        assert ctx.execution_mode == ExecutionMode.LIVE
        assert ctx.source_role == ""
        assert ctx.compactness is None

    def test_with_view_and_mode(self) -> None:
        op = MemoryOperation(action=MemoryAction.WRITE)
        ctx = MemoryPolicyContext(
            operation=op,
            memory_view=MemoryView.VERIFIED_ARCHIVE,
            execution_mode=ExecutionMode.CONSOLIDATION,
            source_role="consolidator",
            compactness=CompactnessConstraints(max_packet_tokens=500),
        )
        assert ctx.memory_view == MemoryView.VERIFIED_ARCHIVE
        assert ctx.execution_mode == ExecutionMode.CONSOLIDATION
        assert ctx.source_role == "consolidator"
        assert ctx.compactness is not None
        assert ctx.compactness.max_packet_tokens == 500

    def test_backward_compat_no_new_fields(self) -> None:
        op = _op()
        ctx = MemoryPolicyContext(operation=op, chain_id="c1", trust_level="trusted")
        assert ctx.chain_id == "c1"
        assert ctx.trust_level == "trusted"
        # New fields have safe defaults
        assert ctx.memory_view == MemoryView.LOCAL_WORKING
        assert ctx.execution_mode == ExecutionMode.LIVE


# ---------------------------------------------------------------------------
# MemoryGovernanceDecision -- v3.6.0 fields
# ---------------------------------------------------------------------------


class TestMemoryGovernanceDecisionExtensions:
    def test_degrade_with_directive(self) -> None:
        d = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DEGRADE,
            reason="raw replay blocked",
            degrade_directive=DegradeDirective(raw_replay_blocked=True),
        )
        assert d.allowed
        assert d.degrade_directive is not None
        assert d.degrade_directive.raw_replay_blocked is True

    def test_degrade_with_threat_context(self) -> None:
        d = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DEGRADE,
            reason="test",
            threat_context=ThreatContext(
                threat_hypothesis="memory poisoning",
                mitigation_applied="degrade",
            ),
        )
        assert d.threat_context is not None
        assert d.threat_context.threat_hypothesis == "memory poisoning"

    def test_to_audit_dict_includes_degrade(self) -> None:
        d = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DEGRADE,
            reason="test",
            degrade_directive=DegradeDirective(
                mode="compact",
                verified_only=True,
                redacted_fields=("secret",),
            ),
        )
        audit = d.to_audit_dict()
        assert audit["degrade_mode"] == "compact"
        assert audit["degrade_verified_only"] is True
        assert audit["degrade_redacted_fields"] == ["secret"]

    def test_to_audit_dict_includes_threat(self) -> None:
        d = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DENY,
            reason="test",
            threat_context=ThreatContext(
                threat_hypothesis="exfiltration",
                source_trust="untrusted",
            ),
        )
        audit = d.to_audit_dict()
        assert audit["threat_hypothesis"] == "exfiltration"
        assert audit["threat_source_trust"] == "untrusted"

    def test_backward_compat_no_directive(self) -> None:
        d = MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW, reason="ok")
        assert d.degrade_directive is None
        assert d.threat_context is None
        audit = d.to_audit_dict()
        assert "degrade_mode" not in audit
        assert "threat_hypothesis" not in audit

    def test_quarantine_verdict_allowed(self) -> None:
        d = MemoryGovernanceDecision(verdict=GovernanceVerdict.QUARANTINE, reason="q")
        assert d.allowed
        assert not d.denied

    def test_deny_verdict_not_allowed(self) -> None:
        d = MemoryGovernanceDecision(verdict=GovernanceVerdict.DENY, reason="d")
        assert not d.allowed
        assert d.denied


# ---------------------------------------------------------------------------
# _merge_directives (unit tests on the helper directly)
# ---------------------------------------------------------------------------


class TestMergeDirectives:
    def _merge(
        self,
        existing: DegradeDirective | None,
        new: DegradeDirective | None,
    ) -> DegradeDirective | None:
        from veronica_core.memory.governor import _merge_directives

        return _merge_directives(existing, new)

    def test_both_none_returns_none(self) -> None:
        assert self._merge(None, None) is None

    def test_existing_none_returns_new(self) -> None:
        new = DegradeDirective(mode="compact")
        result = self._merge(None, new)
        assert result is new

    def test_new_none_returns_existing(self) -> None:
        existing = DegradeDirective(mode="redact")
        result = self._merge(existing, None)
        assert result is existing

    def test_mode_new_wins_when_non_empty(self) -> None:
        result = self._merge(
            DegradeDirective(mode="compact"),
            DegradeDirective(mode="redact"),
        )
        assert result is not None
        assert result.mode == "redact"

    def test_mode_existing_wins_when_new_empty(self) -> None:
        result = self._merge(
            DegradeDirective(mode="compact"),
            DegradeDirective(mode=""),
        )
        assert result is not None
        assert result.mode == "compact"

    def test_bool_fields_or(self) -> None:
        result = self._merge(
            DegradeDirective(verified_only=True, summary_required=False),
            DegradeDirective(verified_only=False, raw_replay_blocked=True),
        )
        assert result is not None
        assert result.verified_only is True
        assert result.summary_required is False
        assert result.raw_replay_blocked is True

    def test_int_fields_stricter_wins(self) -> None:
        result = self._merge(
            DegradeDirective(max_packet_tokens=100, max_content_size_bytes=512),
            DegradeDirective(max_packet_tokens=200, max_content_size_bytes=256),
        )
        assert result is not None
        assert result.max_packet_tokens == 100  # stricter (smaller) wins
        assert result.max_content_size_bytes == 256  # stricter (smaller) wins

    def test_int_zero_treated_as_no_limit(self) -> None:
        # 0 = no limit; the positive value becomes the effective limit
        result = self._merge(
            DegradeDirective(max_packet_tokens=0),
            DegradeDirective(max_packet_tokens=500),
        )
        assert result is not None
        assert result.max_packet_tokens == 500

    def test_tuple_fields_union_sorted(self) -> None:
        result = self._merge(
            DegradeDirective(
                redacted_fields=("b", "a"),
                allowed_provenance=("verified",),
            ),
            DegradeDirective(
                redacted_fields=("c", "a"),
                allowed_provenance=("unverified",),
            ),
        )
        assert result is not None
        assert result.redacted_fields == ("a", "b", "c")
        assert result.allowed_provenance == ("unverified", "verified")

    def test_namespace_downscoped_new_wins(self) -> None:
        result = self._merge(
            DegradeDirective(namespace_downscoped_to="old_ns"),
            DegradeDirective(namespace_downscoped_to="new_ns"),
        )
        assert result is not None
        assert result.namespace_downscoped_to == "new_ns"

    def test_namespace_downscoped_falls_back_to_existing(self) -> None:
        result = self._merge(
            DegradeDirective(namespace_downscoped_to="old_ns"),
            DegradeDirective(namespace_downscoped_to=""),
        )
        assert result is not None
        assert result.namespace_downscoped_to == "old_ns"


# ---------------------------------------------------------------------------
# Governor degrade directive merging (integration tests)
# ---------------------------------------------------------------------------


class TestGovernorDegradeDirectiveMerging:
    def test_single_degrade_hook_propagates_directive(self) -> None:
        from veronica_core.memory.governor import MemoryGovernor

        class DegradeHook:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DEGRADE,
                    reason="compact needed",
                    policy_id="h1",
                    degrade_directive=DegradeDirective(
                        mode="compact",
                        summary_required=True,
                    ),
                )

            def after_op(self, *a: object, **kw: object) -> None:
                pass

        gov = MemoryGovernor(hooks=[DegradeHook()], fail_closed=False)
        op = _op()
        decision = gov.evaluate(op)
        assert decision.verdict == GovernanceVerdict.DEGRADE
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.summary_required is True
        assert decision.degrade_directive.mode == "compact"

    def test_two_degrade_hooks_merge_directives(self) -> None:
        from veronica_core.memory.governor import MemoryGovernor

        class Hook1:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DEGRADE,
                    reason="h1",
                    policy_id="h1",
                    degrade_directive=DegradeDirective(
                        summary_required=True,
                        redacted_fields=("a",),
                    ),
                )

            def after_op(self, *a: object, **kw: object) -> None:
                pass

        class Hook2:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DEGRADE,
                    reason="h2",
                    policy_id="h2",
                    degrade_directive=DegradeDirective(
                        verified_only=True,
                        redacted_fields=("b",),
                    ),
                )

            def after_op(self, *a: object, **kw: object) -> None:
                pass

        gov = MemoryGovernor(hooks=[Hook1(), Hook2()], fail_closed=False)
        op = _op()
        decision = gov.evaluate(op)
        assert decision.verdict == GovernanceVerdict.DEGRADE
        d = decision.degrade_directive
        assert d is not None
        assert d.summary_required is True   # from Hook1
        assert d.verified_only is True       # from Hook2
        assert set(d.redacted_fields) == {"a", "b"}  # union

    def test_allow_then_degrade_propagates_directive(self) -> None:
        from veronica_core.memory.governor import MemoryGovernor

        class AllowHook:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    reason="ok",
                    policy_id="allow",
                )

            def after_op(self, *a: object, **kw: object) -> None:
                pass

        class DegradeHook:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DEGRADE,
                    reason="degrade",
                    policy_id="degrade",
                    degrade_directive=DegradeDirective(mode="truncate"),
                )

            def after_op(self, *a: object, **kw: object) -> None:
                pass

        gov = MemoryGovernor(hooks=[AllowHook(), DegradeHook()], fail_closed=False)
        op = _op()
        decision = gov.evaluate(op)
        assert decision.verdict == GovernanceVerdict.DEGRADE
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.mode == "truncate"

    def test_deny_short_circuits_ignores_directive(self) -> None:
        from veronica_core.memory.governor import MemoryGovernor

        class DenyHook:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason="denied",
                    policy_id="deny",
                )

            def after_op(self, *a: object, **kw: object) -> None:
                pass

        class DegradeHook:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DEGRADE,
                    reason="degrade",
                    policy_id="degrade",
                    degrade_directive=DegradeDirective(mode="truncate"),
                )

            def after_op(self, *a: object, **kw: object) -> None:
                pass

        gov = MemoryGovernor(hooks=[DenyHook(), DegradeHook()], fail_closed=False)
        op = _op()
        decision = gov.evaluate(op)
        assert decision.verdict == GovernanceVerdict.DENY
        assert decision.degrade_directive is None   # DENY short-circuits

    def test_degrade_hook_without_directive_still_degrades(self) -> None:
        from veronica_core.memory.governor import MemoryGovernor

        class DegradeHookNoDirective:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DEGRADE,
                    reason="degrade without directive",
                    policy_id="h",
                )

            def after_op(self, *a: object, **kw: object) -> None:
                pass

        gov = MemoryGovernor(hooks=[DegradeHookNoDirective()], fail_closed=False)
        op = _op()
        decision = gov.evaluate(op)
        assert decision.verdict == GovernanceVerdict.DEGRADE
        assert decision.degrade_directive is None

    def test_allow_verdict_has_no_directive(self) -> None:
        from veronica_core.memory.governor import MemoryGovernor

        class AllowHook:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    reason="ok",
                    policy_id="allow",
                )

            def after_op(self, *a: object, **kw: object) -> None:
                pass

        gov = MemoryGovernor(hooks=[AllowHook()], fail_closed=False)
        op = _op()
        decision = gov.evaluate(op)
        assert decision.verdict == GovernanceVerdict.ALLOW
        assert decision.degrade_directive is None

    def test_threat_context_propagates_from_worst_verdict_hook(self) -> None:
        from veronica_core.memory.governor import MemoryGovernor

        tc = ThreatContext(threat_hypothesis="injection", source_trust="untrusted")

        class AllowHook:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    reason="ok",
                    policy_id="allow",
                    threat_context=ThreatContext(threat_hypothesis="irrelevant"),
                )

            def after_op(self, *a: object, **kw: object) -> None:
                pass

        class DegradeHook:
            def before_op(self, op: MemoryOperation, ctx: MemoryPolicyContext | None) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DEGRADE,
                    reason="degrade",
                    policy_id="degrade",
                    threat_context=tc,
                )

            def after_op(self, *a: object, **kw: object) -> None:
                pass

        gov = MemoryGovernor(hooks=[AllowHook(), DegradeHook()], fail_closed=False)
        op = _op()
        decision = gov.evaluate(op)
        assert decision.verdict == GovernanceVerdict.DEGRADE
        assert decision.threat_context is tc


class TestMergeLimitEdgeCases:
    """Adversarial: _merge_limit boundary conditions."""

    def _merge(self, d1: DegradeDirective, d2: DegradeDirective) -> DegradeDirective | None:
        from veronica_core.memory.governor import _merge_directives

        return _merge_directives(d1, d2)

    def test_both_zero_returns_zero(self) -> None:
        """Both sides 0 (no limit) -> result is 0 (no limit)."""
        result = self._merge(
            DegradeDirective(max_packet_tokens=0),
            DegradeDirective(max_packet_tokens=0),
        )
        assert result is not None
        assert result.max_packet_tokens == 0

    def test_both_zero_content_size(self) -> None:
        """Both sides 0 for max_content_size_bytes -> 0."""
        result = self._merge(
            DegradeDirective(max_content_size_bytes=0),
            DegradeDirective(max_content_size_bytes=0),
        )
        assert result is not None
        assert result.max_content_size_bytes == 0

    def test_zero_and_positive_returns_positive(self) -> None:
        """0 (no limit) + positive -> positive (the effective limit)."""
        result = self._merge(
            DegradeDirective(max_content_size_bytes=0),
            DegradeDirective(max_content_size_bytes=256),
        )
        assert result is not None
        assert result.max_content_size_bytes == 256
