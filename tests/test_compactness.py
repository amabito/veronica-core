"""Tests for CompactnessEvaluator.

Covers:
- No constraints -> ALLOW
- Hard limit: max_payload_bytes exceeded -> DENY
- Soft limits: packet_tokens, attribute_count -> DEGRADE
- raw_replay_ratio exceeded -> DEGRADE with raw_replay_blocked
- require_compaction_if_over_budget -> DEGRADE with summary_required
- prefer_verified_summary with non-VERIFIED provenance -> DEGRADE with verified_only
- Multiple degrade conditions merge into one DegradeDirective
- Default constraints applied when context has none
- ThreatContext compactness_enforced flag set on DEGRADE
"""

from __future__ import annotations

from veronica_core.memory.compactness import CompactnessEvaluator
from veronica_core.memory.types import (
    CompactnessConstraints,
    ExecutionMode,
    GovernanceVerdict,
    MemoryAction,
    MemoryOperation,
    MemoryPolicyContext,
    MemoryProvenance,
    MemoryView,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _op(
    *,
    size: int = 0,
    provenance: MemoryProvenance = MemoryProvenance.UNKNOWN,
    metadata: dict | None = None,
) -> MemoryOperation:
    return MemoryOperation(
        action=MemoryAction.WRITE,
        resource_id="res-1",
        agent_id="agent-a",
        content_size_bytes=size,
        provenance=provenance,
        metadata=metadata or {},
    )


def _ctx(constraints: CompactnessConstraints | None) -> MemoryPolicyContext:
    return MemoryPolicyContext(
        operation=_op(),
        chain_id="chain-1",
        trust_level="trusted",
        memory_view=MemoryView.LOCAL_WORKING,
        execution_mode=ExecutionMode.LIVE,
        compactness=constraints,
    )


# ---------------------------------------------------------------------------
# No constraints -> ALLOW
# ---------------------------------------------------------------------------


class TestNoConstraints:
    def test_no_constraints_allows(self) -> None:
        ev = CompactnessEvaluator()
        op = _op()
        decision = ev.before_op(op, _ctx(None))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_no_constraints_no_context_allows(self) -> None:
        ev = CompactnessEvaluator()
        op = _op()
        decision = ev.before_op(op, None)
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_policy_id_is_compactness(self) -> None:
        ev = CompactnessEvaluator()
        decision = ev.before_op(_op(), _ctx(None))
        assert decision.policy_id == "compactness"


# ---------------------------------------------------------------------------
# Hard limit: max_payload_bytes -> DENY
# ---------------------------------------------------------------------------


class TestPayloadBytesHardLimit:
    def test_payload_bytes_exceeded_denies(self) -> None:
        constraints = CompactnessConstraints(max_payload_bytes=100)
        ev = CompactnessEvaluator()
        op = _op(size=101)
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DENY

    def test_payload_bytes_exactly_at_limit_allows(self) -> None:
        constraints = CompactnessConstraints(max_payload_bytes=100)
        ev = CompactnessEvaluator()
        op = _op(size=100)
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_payload_bytes_zero_limit_is_no_limit(self) -> None:
        constraints = CompactnessConstraints(max_payload_bytes=0)
        ev = CompactnessEvaluator()
        op = _op(size=999_999)
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_deny_reason_mentions_sizes(self) -> None:
        constraints = CompactnessConstraints(max_payload_bytes=50)
        ev = CompactnessEvaluator()
        op = _op(size=200)
        decision = ev.before_op(op, _ctx(constraints))
        assert "200" in decision.reason
        assert "50" in decision.reason


# ---------------------------------------------------------------------------
# Soft limit: packet_tokens -> DEGRADE
# ---------------------------------------------------------------------------


class TestPacketTokensDegrades:
    def test_packet_tokens_exceeded_degrades(self) -> None:
        constraints = CompactnessConstraints(max_packet_tokens=500)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 501})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DEGRADE

    def test_packet_tokens_exactly_at_limit_allows(self) -> None:
        constraints = CompactnessConstraints(max_packet_tokens=500)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 500})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_packet_tokens_directive_has_max(self) -> None:
        constraints = CompactnessConstraints(max_packet_tokens=300)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 600})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.max_packet_tokens == 300

    def test_packet_tokens_zero_limit_is_no_limit(self) -> None:
        constraints = CompactnessConstraints(max_packet_tokens=0)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 99_999})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# Soft limit: attribute_count -> DEGRADE
# ---------------------------------------------------------------------------


class TestAttributesExceededDegrades:
    def test_attributes_exceeded_degrades(self) -> None:
        constraints = CompactnessConstraints(max_attributes_per_packet=10)
        ev = CompactnessEvaluator()
        op = _op(metadata={"attribute_count": 11})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DEGRADE

    def test_attributes_at_limit_allows(self) -> None:
        constraints = CompactnessConstraints(max_attributes_per_packet=10)
        ev = CompactnessEvaluator()
        op = _op(metadata={"attribute_count": 10})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_attributes_zero_limit_is_no_limit(self) -> None:
        constraints = CompactnessConstraints(max_attributes_per_packet=0)
        ev = CompactnessEvaluator()
        op = _op(metadata={"attribute_count": 10_000})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# raw_replay_ratio -> DEGRADE with raw_replay_blocked
# ---------------------------------------------------------------------------


class TestRawReplayRatioDegrades:
    def test_raw_replay_ratio_exceeded_degrades(self) -> None:
        constraints = CompactnessConstraints(max_raw_replay_ratio=0.5)
        ev = CompactnessEvaluator()
        op = _op(metadata={"raw_replay_ratio": 0.6})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DEGRADE

    def test_raw_replay_ratio_directive_raw_replay_blocked(self) -> None:
        constraints = CompactnessConstraints(max_raw_replay_ratio=0.5)
        ev = CompactnessEvaluator()
        op = _op(metadata={"raw_replay_ratio": 0.9})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.raw_replay_blocked is True

    def test_raw_replay_ratio_at_limit_allows(self) -> None:
        constraints = CompactnessConstraints(max_raw_replay_ratio=0.5)
        ev = CompactnessEvaluator()
        op = _op(metadata={"raw_replay_ratio": 0.5})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_missing_raw_replay_metadata_defaults_zero(self) -> None:
        # raw_replay_ratio not in metadata -> treated as 0.0 -> below any limit
        constraints = CompactnessConstraints(max_raw_replay_ratio=0.5)
        ev = CompactnessEvaluator()
        op = _op()
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# require_compaction_if_over_budget -> summary_required
# ---------------------------------------------------------------------------


class TestRequireCompactionDegrades:
    def test_require_compaction_degrades(self) -> None:
        constraints = CompactnessConstraints(
            max_packet_tokens=100,
            require_compaction_if_over_budget=True,
        )
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 200})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DEGRADE
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.summary_required is True

    def test_require_compaction_no_over_budget_allows(self) -> None:
        constraints = CompactnessConstraints(
            max_packet_tokens=100,
            require_compaction_if_over_budget=True,
        )
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 50})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# prefer_verified_summary -> verified_only
# ---------------------------------------------------------------------------


class TestPreferVerifiedSummaryDegrades:
    def test_prefer_verified_summary_degrades_unknown_provenance(self) -> None:
        constraints = CompactnessConstraints(prefer_verified_summary=True)
        ev = CompactnessEvaluator()
        op = _op(provenance=MemoryProvenance.UNKNOWN)
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DEGRADE
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.verified_only is True

    def test_prefer_verified_summary_degrades_unverified(self) -> None:
        constraints = CompactnessConstraints(prefer_verified_summary=True)
        ev = CompactnessEvaluator()
        op = _op(provenance=MemoryProvenance.UNVERIFIED)
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DEGRADE

    def test_prefer_verified_summary_allows_verified(self) -> None:
        constraints = CompactnessConstraints(prefer_verified_summary=True)
        ev = CompactnessEvaluator()
        op = _op(provenance=MemoryProvenance.VERIFIED)
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# Multiple degrade conditions merge
# ---------------------------------------------------------------------------


class TestMultipleDegradeConditionsMerge:
    def test_multiple_degrade_conditions_merge(self) -> None:
        constraints = CompactnessConstraints(
            max_packet_tokens=100,
            max_attributes_per_packet=5,
            max_raw_replay_ratio=0.3,
            prefer_verified_summary=True,
        )
        ev = CompactnessEvaluator()
        op = _op(
            provenance=MemoryProvenance.UNVERIFIED,
            metadata={
                "packet_tokens": 200,
                "attribute_count": 10,
                "raw_replay_ratio": 0.8,
            },
        )
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DEGRADE
        d = decision.degrade_directive
        assert d is not None
        # All flags should be merged into one directive
        assert d.summary_required is True
        assert d.raw_replay_blocked is True
        assert d.verified_only is True
        assert d.max_packet_tokens == 100

    def test_single_degrade_decision_returned(self) -> None:
        # Verify we get exactly one MemoryGovernanceDecision (not a list)
        constraints = CompactnessConstraints(
            max_packet_tokens=10,
            max_attributes_per_packet=2,
        )
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 20, "attribute_count": 5})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DEGRADE
        # Single directive, not duplicated
        assert decision.degrade_directive is not None


# ---------------------------------------------------------------------------
# Default constraints applied
# ---------------------------------------------------------------------------


class TestDefaultConstraintsApplied:
    def test_default_constraints_applied_when_context_has_none(self) -> None:
        default = CompactnessConstraints(max_payload_bytes=10)
        ev = CompactnessEvaluator(default_constraints=default)
        op = _op(size=20)
        # Context has no compactness -- defaults should kick in
        decision = ev.before_op(op, _ctx(None))
        assert decision.verdict is GovernanceVerdict.DENY

    def test_context_constraints_override_defaults(self) -> None:
        default = CompactnessConstraints(max_payload_bytes=10)
        ev = CompactnessEvaluator(default_constraints=default)
        op = _op(size=20)
        # Context has a more permissive constraint -- it should win
        context_constraints = CompactnessConstraints(max_payload_bytes=100)
        decision = ev.before_op(op, _ctx(context_constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_default_constraints_applied_when_no_context(self) -> None:
        default = CompactnessConstraints(max_payload_bytes=5)
        ev = CompactnessEvaluator(default_constraints=default)
        op = _op(size=50)
        decision = ev.before_op(op, None)
        assert decision.verdict is GovernanceVerdict.DENY


# ---------------------------------------------------------------------------
# ThreatContext compactness_enforced
# ---------------------------------------------------------------------------


class TestDegradeThreatContext:
    def test_degrade_has_threat_context(self) -> None:
        constraints = CompactnessConstraints(max_packet_tokens=10)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 20})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.threat_context is not None
        assert decision.threat_context.compactness_enforced is True

    def test_deny_has_threat_context(self) -> None:
        constraints = CompactnessConstraints(max_payload_bytes=5)
        ev = CompactnessEvaluator()
        op = _op(size=100)
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.threat_context is not None
        assert decision.threat_context.compactness_enforced is True

    def test_allow_has_no_enforce_flag(self) -> None:
        ev = CompactnessEvaluator()
        op = _op()
        decision = ev.before_op(op, _ctx(None))
        # ALLOW has no threat_context or compactness_enforced=False
        if decision.threat_context is not None:
            assert decision.threat_context.compactness_enforced is False

    def test_degrade_directive_mode_is_compact(self) -> None:
        constraints = CompactnessConstraints(max_packet_tokens=10)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 20})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.mode == "compact"


# ---------------------------------------------------------------------------
# after_op is a no-op
# ---------------------------------------------------------------------------


class TestAfterOpNoOp:
    def test_after_op_does_not_raise(self) -> None:
        ev = CompactnessEvaluator()
        op = _op()
        from veronica_core.memory.types import MemoryGovernanceDecision
        decision = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            policy_id="compactness",
            operation=op,
        )
        ev.after_op(op, decision)  # must not raise
        ev.after_op(op, decision, result="ok", error=ValueError("boom"))  # must not raise
