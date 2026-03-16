"""Unit tests for security.side_effects module.

Tests cover SideEffectClass severity, SideEffectProfile properties,
classify_action(), and the immutability contracts of module-level mappings.
"""

from __future__ import annotations

import pytest

from veronica_core.security.side_effects import (
    ACTION_SIDE_EFFECTS,
    NO_EFFECT_PROFILE,
    SIDE_EFFECT_SEVERITY,
    SideEffectClass,
    SideEffectProfile,
    classify_action,
    side_effect_severity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile(*classes: SideEffectClass, strict: bool = True) -> SideEffectProfile:
    return SideEffectProfile(effects=frozenset(classes), strict_mode=strict)


# ---------------------------------------------------------------------------
# SideEffectClass severity values
# ---------------------------------------------------------------------------


class TestSideEffectClassSeverity:
    """Each SideEffectClass maps to the correct numeric severity."""

    def test_none_is_zero(self) -> None:
        assert side_effect_severity(SideEffectClass.NONE.value) == 0

    def test_informational_is_one(self) -> None:
        assert side_effect_severity(SideEffectClass.INFORMATIONAL.value) == 1

    def test_read_local_is_two(self) -> None:
        assert side_effect_severity(SideEffectClass.READ_LOCAL.value) == 2

    def test_write_local_is_four(self) -> None:
        assert side_effect_severity(SideEffectClass.WRITE_LOCAL.value) == 4

    def test_shell_execute_is_six(self) -> None:
        assert side_effect_severity(SideEffectClass.SHELL_EXECUTE.value) == 6

    def test_outbound_network_is_six(self) -> None:
        assert side_effect_severity(SideEffectClass.OUTBOUND_NETWORK.value) == 6

    def test_external_mutation_is_eight(self) -> None:
        assert side_effect_severity(SideEffectClass.EXTERNAL_MUTATION.value) == 8

    def test_credential_access_is_eight(self) -> None:
        assert side_effect_severity(SideEffectClass.CREDENTIAL_ACCESS.value) == 8

    def test_cross_agent_is_five(self) -> None:
        assert side_effect_severity(SideEffectClass.CROSS_AGENT.value) == 5

    def test_irreversible_is_ten(self) -> None:
        assert side_effect_severity(SideEffectClass.IRREVERSIBLE.value) == 10

    def test_unknown_effect_returns_max_severity(self) -> None:
        """Unknown effects fall-closed: severity is max (10)."""
        assert side_effect_severity("totally_unknown") == 10

    def test_unknown_string_not_in_severity_map(self) -> None:
        assert "nonexistent_effect" not in SIDE_EFFECT_SEVERITY


# ---------------------------------------------------------------------------
# SideEffectProfile -- max_severity
# ---------------------------------------------------------------------------


class TestSideEffectProfileMaxSeverity:
    def test_empty_effects_max_severity_is_zero(self) -> None:
        assert NO_EFFECT_PROFILE.max_severity == 0

    def test_single_effect_max_severity(self) -> None:
        p = _profile(SideEffectClass.WRITE_LOCAL)
        assert p.max_severity == 4

    def test_multiple_effects_max_severity_is_highest(self) -> None:
        # shell_execute (6) + read_local (2) -- max should be 6
        p = _profile(SideEffectClass.SHELL_EXECUTE, SideEffectClass.READ_LOCAL)
        assert p.max_severity == 6

    def test_irreversible_dominates(self) -> None:
        p = _profile(SideEffectClass.READ_LOCAL, SideEffectClass.IRREVERSIBLE)
        assert p.max_severity == 10


# ---------------------------------------------------------------------------
# SideEffectProfile -- has_write
# ---------------------------------------------------------------------------


class TestSideEffectProfileHasWrite:
    def test_write_local_detected(self) -> None:
        p = _profile(SideEffectClass.WRITE_LOCAL)
        assert p.has_write is True

    def test_shell_execute_detected_as_write(self) -> None:
        p = _profile(SideEffectClass.SHELL_EXECUTE)
        assert p.has_write is True

    def test_read_local_not_write(self) -> None:
        p = _profile(SideEffectClass.READ_LOCAL)
        assert p.has_write is False

    def test_outbound_network_not_write(self) -> None:
        p = _profile(SideEffectClass.OUTBOUND_NETWORK)
        assert p.has_write is False

    def test_empty_not_write(self) -> None:
        assert NO_EFFECT_PROFILE.has_write is False


# ---------------------------------------------------------------------------
# SideEffectProfile -- has_external
# ---------------------------------------------------------------------------


class TestSideEffectProfileHasExternal:
    def test_outbound_network_is_external(self) -> None:
        p = _profile(SideEffectClass.OUTBOUND_NETWORK)
        assert p.has_external is True

    def test_external_mutation_is_external(self) -> None:
        p = _profile(SideEffectClass.EXTERNAL_MUTATION)
        assert p.has_external is True

    def test_cross_agent_is_external(self) -> None:
        p = _profile(SideEffectClass.CROSS_AGENT)
        assert p.has_external is True

    def test_write_local_not_external(self) -> None:
        p = _profile(SideEffectClass.WRITE_LOCAL)
        assert p.has_external is False

    def test_shell_execute_not_external(self) -> None:
        p = _profile(SideEffectClass.SHELL_EXECUTE)
        assert p.has_external is False

    def test_empty_not_external(self) -> None:
        assert NO_EFFECT_PROFILE.has_external is False


# ---------------------------------------------------------------------------
# SideEffectProfile -- has_dangerous
# ---------------------------------------------------------------------------


class TestSideEffectProfileHasDangerous:
    def test_shell_execute_is_dangerous(self) -> None:
        p = _profile(SideEffectClass.SHELL_EXECUTE)
        assert p.has_dangerous is True

    def test_outbound_network_is_dangerous(self) -> None:
        p = _profile(SideEffectClass.OUTBOUND_NETWORK)
        assert p.has_dangerous is True

    def test_external_mutation_is_dangerous(self) -> None:
        p = _profile(SideEffectClass.EXTERNAL_MUTATION)
        assert p.has_dangerous is True

    def test_irreversible_is_dangerous(self) -> None:
        p = _profile(SideEffectClass.IRREVERSIBLE)
        assert p.has_dangerous is True

    def test_write_local_not_dangerous(self) -> None:
        # write_local severity 4 < 6
        p = _profile(SideEffectClass.WRITE_LOCAL)
        assert p.has_dangerous is False

    def test_read_local_not_dangerous(self) -> None:
        p = _profile(SideEffectClass.READ_LOCAL)
        assert p.has_dangerous is False

    def test_empty_not_dangerous(self) -> None:
        assert NO_EFFECT_PROFILE.has_dangerous is False


# ---------------------------------------------------------------------------
# SideEffectProfile -- is_read_only
# ---------------------------------------------------------------------------


class TestSideEffectProfileIsReadOnly:
    def test_read_local_is_read_only(self) -> None:
        p = _profile(SideEffectClass.READ_LOCAL)
        assert p.is_read_only is True

    def test_informational_is_read_only(self) -> None:
        p = _profile(SideEffectClass.INFORMATIONAL)
        assert p.is_read_only is True

    def test_none_class_is_read_only(self) -> None:
        p = _profile(SideEffectClass.NONE)
        assert p.is_read_only is True

    def test_empty_effects_is_read_only(self) -> None:
        assert NO_EFFECT_PROFILE.is_read_only is True

    def test_write_local_not_read_only(self) -> None:
        p = _profile(SideEffectClass.WRITE_LOCAL)
        assert p.is_read_only is False

    def test_shell_execute_not_read_only(self) -> None:
        p = _profile(SideEffectClass.SHELL_EXECUTE)
        assert p.is_read_only is False

    def test_mixed_read_write_not_read_only(self) -> None:
        p = _profile(SideEffectClass.READ_LOCAL, SideEffectClass.WRITE_LOCAL)
        assert p.is_read_only is False


# ---------------------------------------------------------------------------
# SideEffectProfile -- audit_summary
# ---------------------------------------------------------------------------


class TestSideEffectProfileAuditSummary:
    def test_empty_effects_returns_none_string(self) -> None:
        assert NO_EFFECT_PROFILE.audit_summary == "none"

    def test_single_effect_audit_summary(self) -> None:
        p = _profile(SideEffectClass.READ_LOCAL)
        assert p.audit_summary == SideEffectClass.READ_LOCAL.value

    def test_multiple_effects_sorted(self) -> None:
        p = _profile(SideEffectClass.WRITE_LOCAL, SideEffectClass.READ_LOCAL)
        parts = p.audit_summary.split(",")
        assert parts == sorted(parts)

    def test_audit_summary_comma_separated(self) -> None:
        p = _profile(SideEffectClass.SHELL_EXECUTE, SideEffectClass.OUTBOUND_NETWORK)
        summary = p.audit_summary
        assert "," in summary
        assert SideEffectClass.SHELL_EXECUTE.value in summary
        assert SideEffectClass.OUTBOUND_NETWORK.value in summary


# ---------------------------------------------------------------------------
# classify_action
# ---------------------------------------------------------------------------


class TestClassifyAction:
    def test_shell_returns_shell_execute_profile(self) -> None:
        p = classify_action("shell")
        assert SideEffectClass.SHELL_EXECUTE in p.effects

    def test_file_read_returns_read_local_profile(self) -> None:
        p = classify_action("file_read")
        assert SideEffectClass.READ_LOCAL in p.effects

    def test_file_write_returns_write_local_profile(self) -> None:
        p = classify_action("file_write")
        assert SideEffectClass.WRITE_LOCAL in p.effects

    def test_unknown_action_returns_empty_effects(self) -> None:
        p = classify_action("completely_unknown_action_xyz")
        assert len(p.effects) == 0

    def test_unknown_action_strict_mode_true(self) -> None:
        p = classify_action("unknown_action_abc")
        assert p.strict_mode is True

    def test_unknown_action_description_mentions_action_name(self) -> None:
        action = "my_custom_action"
        p = classify_action(action)
        assert action in p.description

    def test_git_push_has_external_mutation_and_network(self) -> None:
        p = classify_action("git_push")
        assert SideEffectClass.EXTERNAL_MUTATION in p.effects
        assert SideEffectClass.OUTBOUND_NETWORK in p.effects

    def test_net_request_returns_outbound_network(self) -> None:
        p = classify_action("net_request")
        assert SideEffectClass.OUTBOUND_NETWORK in p.effects

    def test_browser_navigate_has_network_and_read(self) -> None:
        p = classify_action("browser_navigate")
        assert SideEffectClass.OUTBOUND_NETWORK in p.effects
        assert SideEffectClass.READ_LOCAL in p.effects

    def test_git_commit_write_local(self) -> None:
        p = classify_action("git_commit")
        assert SideEffectClass.WRITE_LOCAL in p.effects


# ---------------------------------------------------------------------------
# Immutability contracts
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_side_effect_severity_is_mapping_proxy(self) -> None:
        from types import MappingProxyType

        assert isinstance(SIDE_EFFECT_SEVERITY, MappingProxyType)

    def test_side_effect_severity_rejects_mutation(self) -> None:
        with pytest.raises(TypeError):
            SIDE_EFFECT_SEVERITY["new_key"] = 99  # type: ignore[index]

    def test_action_side_effects_is_mapping_proxy(self) -> None:
        from types import MappingProxyType

        assert isinstance(ACTION_SIDE_EFFECTS, MappingProxyType)

    def test_action_side_effects_rejects_mutation(self) -> None:
        with pytest.raises(TypeError):
            ACTION_SIDE_EFFECTS["new_action"] = NO_EFFECT_PROFILE  # type: ignore[index]

    def test_side_effect_profile_is_frozen(self) -> None:
        p = _profile(SideEffectClass.READ_LOCAL)
        with pytest.raises((AttributeError, TypeError)):
            p.description = "hacked"  # type: ignore[misc]

    def test_side_effect_profile_effects_is_frozenset(self) -> None:
        p = _profile(SideEffectClass.READ_LOCAL)
        assert isinstance(p.effects, frozenset)

    def test_profile_metadata_is_immutable_mapping(self) -> None:
        from types import MappingProxyType

        p = SideEffectProfile(
            effects=frozenset(),
            metadata={"key": "value"},
        )
        assert isinstance(p.metadata, MappingProxyType)

    def test_profile_set_coerced_to_frozenset(self) -> None:
        """A plain set passed as effects is coerced to frozenset."""
        p = SideEffectProfile(
            effects=frozenset({SideEffectClass.READ_LOCAL}),
        )
        assert isinstance(p.effects, frozenset)
