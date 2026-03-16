"""Integration tests for side-effect classification with built-in policies.

Tests verify that SideEffectProfile interacts correctly with the built-in
policy classes: NoShellPolicy, NoNetworkPolicy, ReadOnlyAssistantPolicy,
and ApproveSideEffectsPolicy.

Side-effect classification is additive information: policies can use it
to make finer-grained decisions, but must not break when it is absent
(backward compatibility).
"""

from __future__ import annotations

from veronica_core.policies.approve_side_effects import ApproveSideEffectsPolicy
from veronica_core.policies.no_network import NoNetworkPolicy
from veronica_core.policies.no_shell import NoShellPolicy
from veronica_core.policies.read_only_assistant import ReadOnlyAssistantPolicy
from veronica_core.security.side_effects import (
    SideEffectClass,
    SideEffectProfile,
    classify_action,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile(*classes: SideEffectClass) -> SideEffectProfile:
    return SideEffectProfile(effects=frozenset(classes))


def _dangerous_profile() -> SideEffectProfile:
    return _profile(SideEffectClass.IRREVERSIBLE)


def _write_profile() -> SideEffectProfile:
    return _profile(SideEffectClass.WRITE_LOCAL)


def _read_profile() -> SideEffectProfile:
    return _profile(SideEffectClass.READ_LOCAL)


def _shell_profile() -> SideEffectProfile:
    return _profile(SideEffectClass.SHELL_EXECUTE)


def _network_profile() -> SideEffectProfile:
    return _profile(SideEffectClass.OUTBOUND_NETWORK)


# ---------------------------------------------------------------------------
# NoShellPolicy + shell_execute side effects
# ---------------------------------------------------------------------------


class TestNoShellPolicyWithSideEffects:
    def test_enabled_shell_action_is_denied(self) -> None:
        policy = NoShellPolicy(enabled=True)
        allowed, reason = policy.check_shell(["bash", "-c", "echo hi"])
        assert allowed is False
        assert "bash" in reason

    def test_disabled_policy_allows_regardless_of_side_effect(self) -> None:
        policy = NoShellPolicy(enabled=False)
        allowed, reason = policy.check_shell(["bash", "-c", "rm -rf /"])
        assert allowed is True

    def test_classify_action_shell_produces_shell_profile(self) -> None:
        """classify_action for 'shell' returns shell_execute -- matches NoShellPolicy intent."""
        se = classify_action("shell")
        assert SideEffectClass.SHELL_EXECUTE in se.effects

    def test_allowlisted_shell_bypasses_block(self) -> None:
        policy = NoShellPolicy(enabled=True, allowlist=frozenset({"ls"}))
        allowed, reason = policy.check_shell(["ls", "-la"])
        assert allowed is True

    def test_unknown_action_side_effect_strict_mode(self) -> None:
        """Unknown action profile has strict_mode=True -- caller should require approval."""
        se = classify_action("totally_unknown_action")
        assert se.strict_mode is True


# ---------------------------------------------------------------------------
# NoNetworkPolicy + outbound_network side effects
# ---------------------------------------------------------------------------


class TestNoNetworkPolicyWithSideEffects:
    def test_enabled_egress_denied(self) -> None:
        policy = NoNetworkPolicy(enabled=True)
        allowed, reason = policy.check_egress("https://example.com")
        assert allowed is False

    def test_network_profile_is_external(self) -> None:
        se = _network_profile()
        assert se.has_external is True

    def test_network_profile_is_dangerous(self) -> None:
        se = _network_profile()
        assert se.has_dangerous is True

    def test_disabled_policy_allows_network_action(self) -> None:
        policy = NoNetworkPolicy(enabled=False)
        allowed, _ = policy.check_egress("https://example.com")
        assert allowed is True

    def test_git_push_profile_has_both_effects(self) -> None:
        """git_push has external_mutation + outbound_network -- NoNetworkPolicy should block."""
        se = classify_action("git_push")
        assert se.has_external is True
        assert SideEffectClass.OUTBOUND_NETWORK in se.effects


# ---------------------------------------------------------------------------
# ReadOnlyAssistantPolicy + write side effects
# ---------------------------------------------------------------------------


class TestReadOnlyAssistantPolicyWithSideEffects:
    def test_write_side_effect_profile_is_not_read_only(self) -> None:
        se = _write_profile()
        assert se.is_read_only is False

    def test_read_side_effect_profile_is_read_only(self) -> None:
        se = _read_profile()
        assert se.is_read_only is True

    def test_file_write_action_is_blocked(self) -> None:
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, reason = policy.check_file_write("/tmp/test.txt")
        assert allowed is False
        assert "test.txt" in reason

    def test_shell_write_command_is_blocked(self) -> None:
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, reason = policy.check_shell(["rm", "-rf", "/tmp/data"])
        assert allowed is False

    def test_shell_read_command_is_allowed(self) -> None:
        policy = ReadOnlyAssistantPolicy(enabled=True)
        se = _read_profile()
        allowed, reason = policy.check_shell(["cat", "file.txt"], side_effects=se)
        assert allowed is True

    def test_post_request_is_blocked_by_read_only_policy(self) -> None:
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, reason = policy.check_egress("https://example.com/api", method="POST")
        assert allowed is False

    def test_get_request_allowed_by_read_only_policy(self) -> None:
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, reason = policy.check_egress("https://example.com/api", method="GET")
        assert allowed is True

    def test_disabled_policy_allows_writes(self) -> None:
        policy = ReadOnlyAssistantPolicy(enabled=False)
        allowed, _ = policy.check_file_write("/etc/passwd")
        assert allowed is True


# ---------------------------------------------------------------------------
# ApproveSideEffectsPolicy + side effects
# ---------------------------------------------------------------------------


class TestApproveSideEffectsPolicyWithSideEffects:
    def test_dangerous_profile_shell_requires_approval(self) -> None:
        """Shell write command requires prior approval when ApproveSideEffectsPolicy enabled."""
        policy = ApproveSideEffectsPolicy(enabled=True)
        allowed, reason = policy.check_shell(["rm", "-rf", "/tmp"])
        assert allowed is False
        assert "approval" in reason.lower()

    def test_write_egress_requires_approval(self) -> None:
        policy = ApproveSideEffectsPolicy(enabled=True)
        allowed, reason = policy.check_egress("https://example.com/api", method="POST")
        assert allowed is False
        assert "approval" in reason.lower()

    def test_read_egress_auto_approved(self) -> None:
        policy = ApproveSideEffectsPolicy(enabled=True)
        allowed, reason = policy.check_egress("https://example.com/api", method="GET")
        assert allowed is True

    def test_file_write_requires_approval(self) -> None:
        policy = ApproveSideEffectsPolicy(enabled=True)
        allowed, reason = policy.check_file_write("/tmp/output.txt")
        assert allowed is False

    def test_file_write_approved_then_allowed(self) -> None:
        policy = ApproveSideEffectsPolicy(enabled=True)
        op = "WRITE:/tmp/output.txt"
        token = policy.request_approval(op)
        policy.record_approval(op, token)
        allowed, reason = policy.check_file_write("/tmp/output.txt")
        assert allowed is True

    def test_write_side_effect_profile_marks_as_dangerous_when_above_six(self) -> None:
        """shell_execute severity 6 -- has_dangerous is True."""
        se = _shell_profile()
        assert se.has_dangerous is True

    def test_disabled_approve_policy_allows_all(self) -> None:
        policy = ApproveSideEffectsPolicy(enabled=False)
        allowed, _ = policy.check_file_write("/etc/shadow")
        assert allowed is True


# ---------------------------------------------------------------------------
# Backward compatibility: no side_effects field does not break evaluation
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_no_shell_check_without_side_effects_kwarg(self) -> None:
        """NoShellPolicy.check_shell accepts no side_effects kwarg -- does not raise."""
        policy = NoShellPolicy(enabled=True)
        allowed, reason = policy.check_shell(["ls"])
        # Just verify it runs; the verdict itself depends on allowlist state.
        assert isinstance(allowed, bool)

    def test_no_network_check_without_side_effects_kwarg(self) -> None:
        policy = NoNetworkPolicy(enabled=True)
        allowed, reason = policy.check_egress("https://example.com")
        assert isinstance(allowed, bool)

    def test_read_only_shell_check_with_none_side_effects(self) -> None:
        """Passing side_effects=None is explicitly valid for API compatibility."""
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, reason = policy.check_shell(["cat", "file.txt"], side_effects=None)
        assert allowed is True

    def test_read_only_egress_check_with_none_side_effects(self) -> None:
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, reason = policy.check_egress(
            "https://example.com", method="GET", side_effects=None
        )
        assert allowed is True


# ---------------------------------------------------------------------------
# Side-effect profile propagation -- metadata in classify_action
# ---------------------------------------------------------------------------


class TestSideEffectProfileMetadata:
    def test_profile_has_description(self) -> None:
        p = classify_action("shell")
        assert isinstance(p.description, str)
        assert len(p.description) > 0

    def test_irreversible_profile_max_severity_is_max(self) -> None:
        p = _dangerous_profile()
        assert p.max_severity == 10

    def test_cross_agent_profile_is_external(self) -> None:
        p = _profile(SideEffectClass.CROSS_AGENT)
        assert p.has_external is True

    def test_combined_authority_and_side_effect_info(self) -> None:
        """Side-effect profile combines with action classification without error."""
        se = classify_action("file_write")
        assert se.has_write is True
        assert se.is_read_only is False
