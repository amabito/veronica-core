"""Tests for memory policy rule compiler and evaluator.

Covers: valid compilation, invalid/unknown parameter rejection,
        fail-closed semantics, deterministic output, conflicting rules,
        and adversarial inputs.
"""

from __future__ import annotations

import pytest

from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.types import (
    ExecutionMode,
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
    MemoryProvenance,
    MemoryView,
)
from veronica_core.policy.bundle import PolicyRule
from veronica_core.policy.memory_rules import (
    MemoryRuleCompiler,
    MemoryRuleEvaluator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _op(
    action: MemoryAction = MemoryAction.READ,
    namespace: str = "default",
    provenance: MemoryProvenance = MemoryProvenance.UNKNOWN,
) -> MemoryOperation:
    return MemoryOperation(
        action=action,
        resource_id="r",
        agent_id="a",
        namespace=namespace,
        provenance=provenance,
    )


def _ctx(
    op: MemoryOperation | None = None,
    view: MemoryView = MemoryView.LOCAL_WORKING,
    mode: ExecutionMode = ExecutionMode.LIVE,
) -> MemoryPolicyContext:
    return MemoryPolicyContext(
        operation=op or _op(),
        memory_view=view,
        execution_mode=mode,
    )


def _rule(
    rule_id: str = "r1",
    priority: int = 100,
    enabled: bool = True,
    **params: object,
) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        rule_type="memory",
        parameters=dict(params),
        enabled=enabled,
        priority=priority,
    )


# ---------------------------------------------------------------------------
# Compiler -- valid parameters
# ---------------------------------------------------------------------------


class TestCompilerValid:
    """Compiler accepts valid parameters and produces correct CompiledMemoryRule."""

    def test_minimal_rule(self) -> None:
        compiler = MemoryRuleCompiler()
        compiled = compiler.compile(_rule(verdict="allow"))
        assert compiled.rule_id == "r1"
        assert compiled.verdict is GovernanceVerdict.ALLOW
        assert compiled.priority == 100

    def test_action_singular(self) -> None:
        compiled = MemoryRuleCompiler().compile(_rule(action="read", verdict="allow"))
        assert compiled.actions == frozenset({"read"})

    def test_actions_plural(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(actions=["read", "write"], verdict="allow"),
        )
        assert compiled.actions == frozenset({"read", "write"})

    def test_allowed_views(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(allowed_views=["agent_private", "local_working"], verdict="allow"),
        )
        assert compiled.allowed_views == frozenset({"agent_private", "local_working"})

    def test_allowed_modes(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(allowed_modes=["live_execution", "replay"], verdict="allow"),
        )
        assert compiled.allowed_modes == frozenset({"live_execution", "replay"})

    def test_allowed_provenance(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(allowed_provenance=["verified"], verdict="allow"),
        )
        assert compiled.allowed_provenance == frozenset({"verified"})

    def test_namespace_singular(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(namespace="private", verdict="allow"),
        )
        assert compiled.namespaces == frozenset({"private"})

    def test_degrade_constraints(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(
                verdict="degrade",
                max_packet_tokens=500,
                max_raw_replay_ratio=0.5,
                require_compaction_if_over_budget=True,
                verified_only=True,
            ),
        )
        assert compiled.verdict is GovernanceVerdict.DEGRADE
        assert compiled.max_packet_tokens == 500
        assert compiled.max_raw_replay_ratio == 0.5
        assert compiled.require_compaction_if_over_budget is True
        assert compiled.verified_only is True

    def test_bridge_restrictions(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(bridge_allow_archive=False, bridge_require_signature=True, verdict="deny"),
        )
        assert compiled.bridge_allow_archive is False
        assert compiled.bridge_require_signature is True

    def test_default_verdict_is_deny(self) -> None:
        compiled = MemoryRuleCompiler().compile(_rule())
        assert compiled.verdict is GovernanceVerdict.DENY

    def test_compile_bundle_filters_and_sorts(self) -> None:
        rules = [
            _rule(rule_id="low", priority=200, verdict="allow"),
            _rule(rule_id="high", priority=10, verdict="deny"),
            PolicyRule(rule_id="skip", rule_type="budget", parameters={}),
            _rule(rule_id="disabled", enabled=False, verdict="allow"),
        ]
        compiled = MemoryRuleCompiler().compile_bundle(rules)
        assert len(compiled) == 2
        assert compiled[0].rule_id == "high"
        assert compiled[1].rule_id == "low"


# ---------------------------------------------------------------------------
# Compiler -- invalid parameters (fail-closed)
# ---------------------------------------------------------------------------


class TestCompilerInvalid:
    """Compiler rejects invalid/unknown parameters with clear errors."""

    def test_unknown_parameter(self) -> None:
        with pytest.raises(ValueError, match="Unknown memory rule parameters"):
            MemoryRuleCompiler().compile(_rule(bogus_param="x"))

    def test_wrong_rule_type(self) -> None:
        rule = PolicyRule(rule_id="r1", rule_type="budget", parameters={})
        with pytest.raises(ValueError, match="rule_type='memory'"):
            MemoryRuleCompiler().compile(rule)

    def test_invalid_action(self) -> None:
        with pytest.raises(ValueError, match="Invalid values for actions"):
            MemoryRuleCompiler().compile(_rule(action="fly"))

    def test_invalid_view(self) -> None:
        with pytest.raises(ValueError, match="Invalid values for allowed_views"):
            MemoryRuleCompiler().compile(_rule(allowed_views=["nonexistent"]))

    def test_invalid_mode(self) -> None:
        with pytest.raises(ValueError, match="Invalid values for allowed_modes"):
            MemoryRuleCompiler().compile(_rule(allowed_modes=["turbo"]))

    def test_invalid_provenance(self) -> None:
        with pytest.raises(ValueError, match="Invalid values for allowed_provenance"):
            MemoryRuleCompiler().compile(_rule(allowed_provenance=["magic"]))

    def test_invalid_verdict(self) -> None:
        with pytest.raises(ValueError, match="Invalid verdict"):
            MemoryRuleCompiler().compile(_rule(verdict="explode"))

    def test_negative_max_packet_tokens(self) -> None:
        with pytest.raises(ValueError, match="max_packet_tokens must be >= 0"):
            MemoryRuleCompiler().compile(_rule(max_packet_tokens=-1))

    def test_max_raw_replay_ratio_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="max_raw_replay_ratio"):
            MemoryRuleCompiler().compile(_rule(max_raw_replay_ratio=2.0))

    def test_bool_type_check(self) -> None:
        with pytest.raises(TypeError, match="verified_only must be bool"):
            MemoryRuleCompiler().compile(_rule(verified_only="yes"))

    def test_int_type_check(self) -> None:
        with pytest.raises(TypeError, match="max_packet_tokens must be int"):
            MemoryRuleCompiler().compile(_rule(max_packet_tokens="100"))

    def test_verdict_type_check(self) -> None:
        with pytest.raises(TypeError, match="verdict must be a string"):
            MemoryRuleCompiler().compile(_rule(verdict=42))

    def test_actions_item_type_check(self) -> None:
        with pytest.raises(TypeError, match="actions items must be strings"):
            MemoryRuleCompiler().compile(_rule(actions=[123]))


# ---------------------------------------------------------------------------
# Evaluator -- deterministic output
# ---------------------------------------------------------------------------


class TestEvaluatorDeterministic:
    """Evaluator produces deterministic, repeatable results."""

    def test_no_rules_returns_deny(self) -> None:
        evaluator = MemoryRuleEvaluator()
        decision = evaluator.before_op(_op(), None)
        assert decision.verdict is GovernanceVerdict.DENY
        assert "no memory rules" in decision.reason

    def test_matching_allow_rule(self) -> None:
        compiled = MemoryRuleCompiler().compile(_rule(action="read", verdict="allow"))
        evaluator = MemoryRuleEvaluator((compiled,))
        decision = evaluator.before_op(_op(action=MemoryAction.READ), None)
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_matching_deny_rule(self) -> None:
        compiled = MemoryRuleCompiler().compile(_rule(action="write", verdict="deny"))
        evaluator = MemoryRuleEvaluator((compiled,))
        decision = evaluator.before_op(_op(action=MemoryAction.WRITE), None)
        assert decision.verdict is GovernanceVerdict.DENY

    def test_no_matching_rule_returns_deny(self) -> None:
        compiled = MemoryRuleCompiler().compile(_rule(action="write", verdict="allow"))
        evaluator = MemoryRuleEvaluator((compiled,))
        decision = evaluator.before_op(_op(action=MemoryAction.READ), None)
        assert decision.verdict is GovernanceVerdict.DENY
        assert "no matching memory rule" in decision.reason

    def test_degrade_rule_produces_directive(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(action="read", verdict="degrade", max_packet_tokens=200),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        decision = evaluator.before_op(_op(action=MemoryAction.READ), None)
        assert decision.verdict is GovernanceVerdict.DEGRADE
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.max_packet_tokens == 200

    def test_quarantine_rule(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(action="write", verdict="quarantine"),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        decision = evaluator.before_op(_op(action=MemoryAction.WRITE), None)
        assert decision.verdict is GovernanceVerdict.QUARANTINE

    def test_namespace_filter(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(namespace="secret", verdict="deny"),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        assert evaluator.before_op(_op(namespace="secret"), None).denied
        assert evaluator.before_op(_op(namespace="public"), None).denied  # no match -> deny

    def test_provenance_filter(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(allowed_provenance=["verified"], verdict="allow"),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        assert evaluator.before_op(
            _op(provenance=MemoryProvenance.VERIFIED), None,
        ).allowed
        assert evaluator.before_op(
            _op(provenance=MemoryProvenance.UNKNOWN), None,
        ).denied

    def test_view_filter(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(allowed_views=["agent_private"], verdict="allow"),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        op = _op()
        assert evaluator.before_op(
            op, _ctx(op, view=MemoryView.AGENT_PRIVATE),
        ).allowed
        assert evaluator.before_op(
            op, _ctx(op, view=MemoryView.TEAM_SHARED),
        ).denied

    def test_mode_filter(self) -> None:
        compiled = MemoryRuleCompiler().compile(
            _rule(allowed_modes=["replay"], verdict="allow"),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        op = _op()
        assert evaluator.before_op(
            op, _ctx(op, mode=ExecutionMode.REPLAY),
        ).allowed
        assert evaluator.before_op(
            op, _ctx(op, mode=ExecutionMode.LIVE),
        ).denied

    def test_deterministic_repeated_calls(self) -> None:
        compiled = MemoryRuleCompiler().compile(_rule(action="read", verdict="allow"))
        evaluator = MemoryRuleEvaluator((compiled,))
        results = [evaluator.before_op(_op(), None).verdict for _ in range(100)]
        assert all(v is GovernanceVerdict.ALLOW for v in results)


# ---------------------------------------------------------------------------
# Evaluator -- priority ordering and conflicting rules
# ---------------------------------------------------------------------------


class TestEvaluatorConflicting:
    """First matching rule wins; priority determines order."""

    def test_higher_priority_wins(self) -> None:
        compiler = MemoryRuleCompiler()
        deny_rule = compiler.compile(_rule(rule_id="deny_all", priority=10, verdict="deny"))
        allow_rule = compiler.compile(_rule(rule_id="allow_all", priority=100, verdict="allow"))
        evaluator = MemoryRuleEvaluator((deny_rule, allow_rule))
        assert evaluator.before_op(_op(), None).denied

    def test_lower_priority_number_evaluated_first(self) -> None:
        compiler = MemoryRuleCompiler()
        allow_rule = compiler.compile(
            _rule(rule_id="allow_read", priority=10, action="read", verdict="allow"),
        )
        deny_rule = compiler.compile(
            _rule(rule_id="deny_all", priority=100, verdict="deny"),
        )
        evaluator = MemoryRuleEvaluator((allow_rule, deny_rule))
        assert evaluator.before_op(_op(action=MemoryAction.READ), None).allowed

    def test_overlapping_namespace_rules(self) -> None:
        compiler = MemoryRuleCompiler()
        specific = compiler.compile(
            _rule(rule_id="specific", priority=10, namespace="secret", verdict="deny"),
        )
        generic = compiler.compile(
            _rule(rule_id="generic", priority=100, verdict="allow"),
        )
        evaluator = MemoryRuleEvaluator((specific, generic))
        assert evaluator.before_op(_op(namespace="secret"), None).denied
        assert evaluator.before_op(_op(namespace="public"), None).allowed


# ---------------------------------------------------------------------------
# Evaluator -- governor integration
# ---------------------------------------------------------------------------


class TestEvaluatorGovernorIntegration:
    """MemoryRuleEvaluator works as a MemoryGovernanceHook in MemoryGovernor."""

    def test_governor_with_rule_evaluator(self) -> None:
        compiled = MemoryRuleCompiler().compile(_rule(action="read", verdict="allow"))
        evaluator = MemoryRuleEvaluator((compiled,))
        governor = MemoryGovernor(hooks=[evaluator])
        decision = governor.evaluate(_op(action=MemoryAction.READ))
        assert decision.allowed

    def test_rule_count_property(self) -> None:
        compiler = MemoryRuleCompiler()
        rules = compiler.compile_bundle([
            _rule(rule_id="a", verdict="allow"),
            _rule(rule_id="b", verdict="deny"),
        ])
        evaluator = MemoryRuleEvaluator(rules)
        assert evaluator.rule_count == 2


# ---------------------------------------------------------------------------
# Adversarial
# ---------------------------------------------------------------------------


class TestAdversarialCompiler:
    """Adversarial inputs must not crash or bypass fail-closed."""

    def test_bool_as_int_rejected(self) -> None:
        """bool is a subclass of int; compiler must reject it."""
        with pytest.raises(TypeError, match="max_packet_tokens must be int"):
            MemoryRuleCompiler().compile(_rule(max_packet_tokens=True))

    def test_bool_as_float_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a number, got bool"):
            MemoryRuleCompiler().compile(_rule(max_raw_replay_ratio=False))

    def test_empty_actions_list_matches_all(self) -> None:
        compiled = MemoryRuleCompiler().compile(_rule(actions=[], verdict="allow"))
        assert compiled.actions == frozenset()
        evaluator = MemoryRuleEvaluator((compiled,))
        assert evaluator.before_op(_op(action=MemoryAction.WRITE), None).allowed

    def test_actions_string_not_list(self) -> None:
        """Single string in plural param is accepted."""
        compiled = MemoryRuleCompiler().compile(_rule(actions="read", verdict="allow"))
        assert compiled.actions == frozenset({"read"})

    def test_view_filter_fail_closed_without_context(self) -> None:
        """Rule with allowed_views must NOT match when context is None (fail-closed)."""
        compiled = MemoryRuleCompiler().compile(
            _rule(allowed_views=["agent_private"], verdict="allow"),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        # No context -> view constraint cannot be verified -> deny
        assert evaluator.before_op(_op(), None).denied

    def test_mode_filter_fail_closed_without_context(self) -> None:
        """Rule with allowed_modes must NOT match when context is None (fail-closed)."""
        compiled = MemoryRuleCompiler().compile(
            _rule(allowed_modes=["replay"], verdict="allow"),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        assert evaluator.before_op(_op(), None).denied

    def test_after_op_is_noop(self) -> None:
        evaluator = MemoryRuleEvaluator()
        evaluator.after_op(_op(), MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
        ))


# ---------------------------------------------------------------------------
# Adversarial -- Round 2: context manipulation, priority edges, degrade
#                         directive integrity, type/boundary confusion
# ---------------------------------------------------------------------------


class TestAdversarialEvaluatorRound2:
    """Adversarial tests -- attacker mindset, Round 2.

    Targets: context manipulation, priority tie-breaking, degrade directive
    field integrity, and boundary/type confusion not covered in Round 1.
    """

    # ------------------------------------------------------------------
    # Context manipulation
    # ------------------------------------------------------------------

    def test_combined_view_and_mode_constraint_ctx_none_denies(self) -> None:
        """Rule with BOTH allowed_views AND allowed_modes must deny when ctx=None.

        Attacker scenario: provide no context hoping one constraint is skipped.
        Both constraints require a non-None context -- fail-closed on either.
        """
        compiled = MemoryRuleCompiler().compile(
            _rule(
                allowed_views=["agent_private"],
                allowed_modes=["replay"],
                verdict="allow",
            ),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        decision = evaluator.before_op(_op(), None)
        assert decision.denied, (
            "allowed_views+allowed_modes rule must deny when context is absent"
        )

    def test_combined_view_and_mode_constraint_wrong_view_denies(self) -> None:
        """Rule with allowed_views + allowed_modes: wrong view must deny even if mode matches."""
        compiled = MemoryRuleCompiler().compile(
            _rule(
                allowed_views=["agent_private"],
                allowed_modes=["replay"],
                verdict="allow",
            ),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        op = _op()
        # Mode matches (replay), view is wrong (local_working) -> must deny
        ctx_wrong_view = _ctx(op, view=MemoryView.LOCAL_WORKING, mode=ExecutionMode.REPLAY)
        assert evaluator.before_op(op, ctx_wrong_view).denied

    def test_combined_view_and_mode_constraint_correct_context_allows(self) -> None:
        """Same rule as above: both view and mode correct -> must allow."""
        compiled = MemoryRuleCompiler().compile(
            _rule(
                allowed_views=["agent_private"],
                allowed_modes=["replay"],
                verdict="allow",
            ),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        op = _op()
        ctx_correct = _ctx(op, view=MemoryView.AGENT_PRIVATE, mode=ExecutionMode.REPLAY)
        assert evaluator.before_op(op, ctx_correct).allowed

    def test_partial_triple_constraint_does_not_match(self) -> None:
        """Rule with namespace + allowed_views + action: partial match must NOT trigger.

        Only the namespace matches; view and action do not. Must deny (no match -> fail-closed).
        """
        compiled = MemoryRuleCompiler().compile(
            _rule(
                namespace="sensitive",
                allowed_views=["agent_private"],
                action="write",
                verdict="allow",
            ),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        op_ns_only = _op(action=MemoryAction.READ, namespace="sensitive")
        ctx = _ctx(op_ns_only, view=MemoryView.LOCAL_WORKING)
        # Namespace matches, but action=READ (not write) and view=LOCAL_WORKING (not agent_private)
        assert evaluator.before_op(op_ns_only, ctx).denied

    def test_triple_constraint_exact_match_allows(self) -> None:
        """All three constraints must match together for the rule to fire."""
        compiled = MemoryRuleCompiler().compile(
            _rule(
                namespace="sensitive",
                allowed_views=["agent_private"],
                action="write",
                verdict="allow",
            ),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        op = _op(action=MemoryAction.WRITE, namespace="sensitive")
        ctx = _ctx(op, view=MemoryView.AGENT_PRIVATE)
        assert evaluator.before_op(op, ctx).allowed

    # ------------------------------------------------------------------
    # Priority edge cases
    # ------------------------------------------------------------------

    def test_same_priority_tie_broken_alphabetically_by_rule_id(self) -> None:
        """Two rules at the same priority must fire in alphabetical rule_id order.

        compile_bundle sorts by (priority, rule_id), so 'aaa' fires before 'zzz'.
        The first matching rule wins, so aaa's verdict is the result.
        """
        compiler = MemoryRuleCompiler()
        rules = compiler.compile_bundle([
            _rule(rule_id="zzz", priority=50, verdict="allow"),
            _rule(rule_id="aaa", priority=50, verdict="deny"),
        ])
        # Verify sort order: aaa before zzz
        assert rules[0].rule_id == "aaa"
        assert rules[1].rule_id == "zzz"

        evaluator = MemoryRuleEvaluator(rules)
        # Both rules match all ops; aaa (deny) must win over zzz (allow)
        decision = evaluator.before_op(_op(), None)
        assert decision.denied, "alphabetically first rule_id must win the tie"

    def test_priority_zero_is_evaluated_first(self) -> None:
        """priority=0 is the minimum and must sort before any positive priority."""
        compiler = MemoryRuleCompiler()
        rules = compiler.compile_bundle([
            _rule(rule_id="high", priority=1, verdict="allow"),
            _rule(rule_id="zero", priority=0, verdict="deny"),
        ])
        assert rules[0].rule_id == "zero"
        evaluator = MemoryRuleEvaluator(rules)
        assert evaluator.before_op(_op(), None).denied

    def test_very_large_priority_evaluated_last(self) -> None:
        """priority=999999 must sort after lower priority rules."""
        compiler = MemoryRuleCompiler()
        rules = compiler.compile_bundle([
            _rule(rule_id="giant", priority=999999, verdict="allow"),
            _rule(rule_id="small", priority=1, action="read", verdict="deny"),
        ])
        assert rules[0].rule_id == "small"
        evaluator = MemoryRuleEvaluator(rules)
        # READ op: small (p=1, deny) fires first; giant (p=999999, allow) never reached
        assert evaluator.before_op(_op(action=MemoryAction.READ), None).denied
        # WRITE op: small does not match (action=read only); giant (allow) fires
        assert evaluator.before_op(_op(action=MemoryAction.WRITE), None).allowed

    # ------------------------------------------------------------------
    # DEGRADE directive integrity
    # ------------------------------------------------------------------

    def test_degrade_max_raw_replay_ratio_zero_blocks_raw_replay(self) -> None:
        """max_raw_replay_ratio=0.0 -> raw_replay_blocked must be True in directive."""
        compiled = MemoryRuleCompiler().compile(
            _rule(verdict="degrade", max_raw_replay_ratio=0.0),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        decision = evaluator.before_op(_op(), None)
        assert decision.verdict is GovernanceVerdict.DEGRADE
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.raw_replay_blocked is True, (
            "ratio=0.0 < 1.0 must set raw_replay_blocked=True"
        )

    def test_degrade_max_raw_replay_ratio_one_does_not_block_raw_replay(self) -> None:
        """max_raw_replay_ratio=1.0 (default) -> raw_replay_blocked must be False."""
        compiled = MemoryRuleCompiler().compile(
            _rule(verdict="degrade", max_raw_replay_ratio=1.0),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        decision = evaluator.before_op(_op(), None)
        assert decision.verdict is GovernanceVerdict.DEGRADE
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.raw_replay_blocked is False, (
            "ratio=1.0 must NOT set raw_replay_blocked"
        )

    def test_degrade_directive_fields_match_compiled_rule_exactly(self) -> None:
        """Every constraint field in the compiled DEGRADE rule must appear
        unchanged in the produced DegradeDirective -- no silent truncation or
        default substitution.

        verified_only=True requires a VERIFIED provenance operation to match.
        The point of this test is directive field fidelity, not the match path.
        """
        compiled = MemoryRuleCompiler().compile(
            _rule(
                verdict="degrade",
                max_packet_tokens=512,
                max_raw_replay_ratio=0.25,
                require_compaction_if_over_budget=True,
                verified_only=True,
            ),
        )
        evaluator = MemoryRuleEvaluator((compiled,))
        # Must use VERIFIED provenance so verified_only=True does not block the match.
        op_verified = _op(provenance=MemoryProvenance.VERIFIED)
        decision = evaluator.before_op(op_verified, None)

        assert decision.verdict is GovernanceVerdict.DEGRADE
        d = decision.degrade_directive
        assert d is not None
        assert d.max_packet_tokens == compiled.max_packet_tokens, (
            "max_packet_tokens must be forwarded unchanged"
        )
        assert d.verified_only == compiled.verified_only, (
            "verified_only must be forwarded unchanged"
        )
        assert d.summary_required == compiled.require_compaction_if_over_budget, (
            "require_compaction_if_over_budget maps to summary_required"
        )
        # 0.25 < 1.0 -> blocked
        assert d.raw_replay_blocked == (compiled.max_raw_replay_ratio < 1.0), (
            "raw_replay_blocked must equal (max_raw_replay_ratio < 1.0)"
        )

    # ------------------------------------------------------------------
    # Type confusion / boundary
    # ------------------------------------------------------------------

    def test_memory_operation_with_content_size_bytes_zero(self) -> None:
        """content_size_bytes=0 is the boundary minimum; evaluator must handle it
        without error and apply normal verdict logic.
        """
        op_zero = MemoryOperation(
            action=MemoryAction.READ,
            resource_id="r",
            agent_id="a",
            namespace="default",
            provenance=MemoryProvenance.UNKNOWN,
            content_size_bytes=0,
        )
        compiled = MemoryRuleCompiler().compile(_rule(action="read", verdict="allow"))
        evaluator = MemoryRuleEvaluator((compiled,))
        decision = evaluator.before_op(op_zero, None)
        assert decision.allowed, (
            "content_size_bytes=0 is valid and must not cause unexpected denial"
        )

    def test_single_allow_rule_produces_no_threat_context(self) -> None:
        """An ALLOW verdict must NOT attach a ThreatContext -- no false alarms.

        Only DENY, QUARANTINE, and DEGRADE verdicts should produce threat_context.
        """
        compiled = MemoryRuleCompiler().compile(_rule(action="read", verdict="allow"))
        evaluator = MemoryRuleEvaluator((compiled,))
        decision = evaluator.before_op(_op(action=MemoryAction.READ), None)
        assert decision.verdict is GovernanceVerdict.ALLOW
        assert decision.threat_context is None, (
            "ALLOW verdict must not produce a ThreatContext (no false threat alarms)"
        )
