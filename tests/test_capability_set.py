"""Tests for CapabilitySet factory methods and has_cap() helper.

Covers:
- CapabilitySet.dev(), .ci(), .audit() factory methods
- has_cap() function
"""

from __future__ import annotations

import pytest

from veronica_core.security.capabilities import Capability, CapabilitySet, has_cap


# ---------------------------------------------------------------------------
# T1: CapabilitySet factory methods
# ---------------------------------------------------------------------------


class TestCapabilitySetFactories:
    """Tests for CapabilitySet.dev(), .ci(), .audit() factory methods."""

    def test_dev_returns_capability_set(self) -> None:
        """CapabilitySet.dev() returns a CapabilitySet instance."""
        cs = CapabilitySet.dev()
        assert isinstance(cs, CapabilitySet)

    def test_dev_contains_expected_caps(self) -> None:
        """dev() profile includes READ_REPO, EDIT_REPO, BUILD, TEST, SHELL_BASIC."""
        cs = CapabilitySet.dev()
        assert Capability.READ_REPO in cs.caps
        assert Capability.EDIT_REPO in cs.caps
        assert Capability.BUILD in cs.caps
        assert Capability.TEST in cs.caps
        assert Capability.SHELL_BASIC in cs.caps

    def test_dev_does_not_include_net_fetch_or_sensitive(self) -> None:
        """dev() profile must NOT include NET_FETCH_ALLOWLIST or FILE_READ_SENSITIVE."""
        cs = CapabilitySet.dev()
        assert Capability.NET_FETCH_ALLOWLIST not in cs.caps
        assert Capability.FILE_READ_SENSITIVE not in cs.caps

    def test_ci_returns_capability_set(self) -> None:
        """CapabilitySet.ci() returns a CapabilitySet instance."""
        cs = CapabilitySet.ci()
        assert isinstance(cs, CapabilitySet)

    def test_ci_contains_expected_caps(self) -> None:
        """ci() profile includes READ_REPO, BUILD, TEST."""
        cs = CapabilitySet.ci()
        assert Capability.READ_REPO in cs.caps
        assert Capability.BUILD in cs.caps
        assert Capability.TEST in cs.caps

    def test_ci_excludes_edit_and_shell(self) -> None:
        """ci() profile must NOT include EDIT_REPO or SHELL_BASIC."""
        cs = CapabilitySet.ci()
        assert Capability.EDIT_REPO not in cs.caps
        assert Capability.SHELL_BASIC not in cs.caps

    def test_audit_returns_capability_set(self) -> None:
        """CapabilitySet.audit() returns a CapabilitySet instance."""
        cs = CapabilitySet.audit()
        assert isinstance(cs, CapabilitySet)

    def test_audit_contains_only_read_repo(self) -> None:
        """audit() profile contains exactly READ_REPO and nothing else."""
        cs = CapabilitySet.audit()
        assert cs.caps == frozenset({Capability.READ_REPO})

    def test_audit_excludes_all_write_caps(self) -> None:
        """audit() must not include EDIT_REPO, BUILD, TEST, SHELL_BASIC."""
        cs = CapabilitySet.audit()
        for cap in (
            Capability.EDIT_REPO,
            Capability.BUILD,
            Capability.TEST,
            Capability.SHELL_BASIC,
            Capability.GIT_PUSH_APPROVAL,
        ):
            assert cap not in cs.caps

    def test_factory_returns_frozen_dataclass(self) -> None:
        """CapabilitySet is frozen; mutation must raise AttributeError."""
        cs = CapabilitySet.dev()
        with pytest.raises((AttributeError, TypeError)):
            cs.caps = frozenset()  # type: ignore[misc]

    def test_factory_caps_are_frozenset(self) -> None:
        """caps attribute must be a frozenset for all factory methods."""
        for method in (CapabilitySet.dev, CapabilitySet.ci, CapabilitySet.audit):
            cs = method()
            assert isinstance(cs.caps, frozenset)


# ---------------------------------------------------------------------------
# T1: has_cap() helper
# ---------------------------------------------------------------------------


class TestHasCap:
    """Tests for the has_cap() helper function."""

    def test_has_cap_present_returns_true(self) -> None:
        """has_cap() returns True when capability is present."""
        cs = CapabilitySet.dev()
        assert has_cap(cs, Capability.READ_REPO) is True
        assert has_cap(cs, Capability.BUILD) is True

    def test_has_cap_absent_returns_false(self) -> None:
        """has_cap() returns False when capability is absent."""
        cs = CapabilitySet.audit()
        assert has_cap(cs, Capability.EDIT_REPO) is False
        assert has_cap(cs, Capability.SHELL_BASIC) is False

    def test_has_cap_empty_set_always_false(self) -> None:
        """has_cap() returns False for an empty CapabilitySet."""
        cs = CapabilitySet(caps=frozenset())
        for cap in Capability:
            assert has_cap(cs, cap) is False

    def test_has_cap_all_caps_in_full_set(self) -> None:
        """has_cap() returns True for every Capability when all caps are granted."""
        cs = CapabilitySet(caps=frozenset(Capability))
        for cap in Capability:
            assert has_cap(cs, cap) is True
