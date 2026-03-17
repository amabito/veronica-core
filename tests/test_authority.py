"""Unit tests for veronica_core.security.authority."""

from __future__ import annotations

import time
from types import MappingProxyType

import pytest

from veronica_core.security.authority import (
    AUTHORITY_TRUST_CEILING,
    UNKNOWN_AUTHORITY,
    AuthorityClaim,
    AuthoritySource,
    is_low_authority,
    is_policy_authority,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _claim(source: AuthoritySource, asserted: str = "") -> AuthorityClaim:
    return AuthorityClaim(source=source, asserted_trust=asserted)


# ---------------------------------------------------------------------------
# AuthoritySource enum
# ---------------------------------------------------------------------------


class TestAuthoritySourceEnum:
    def test_all_sources_present(self) -> None:
        expected = {
            "developer_policy",
            "system_config",
            "user_input",
            "tool_output",
            "retrieved_content",
            "memory_content",
            "agent_generated",
            "external_message",
            "approved_override",
            "unknown",
        }
        values = {s.value for s in AuthoritySource}
        assert values == expected

    def test_source_is_str_subclass(self) -> None:
        # AuthoritySource inherits from str -- values can be used as dict keys
        assert isinstance(AuthoritySource.DEVELOPER_POLICY, str)
        assert AuthoritySource.DEVELOPER_POLICY == "developer_policy"

    def test_enum_membership(self) -> None:
        assert AuthoritySource("tool_output") is AuthoritySource.TOOL_OUTPUT


# ---------------------------------------------------------------------------
# AUTHORITY_TRUST_CEILING
# ---------------------------------------------------------------------------


class TestAuthorityTrustCeiling:
    def test_is_mapping_proxy(self) -> None:
        assert isinstance(AUTHORITY_TRUST_CEILING, MappingProxyType)

    def test_ceiling_is_immutable(self) -> None:
        with pytest.raises(TypeError):
            AUTHORITY_TRUST_CEILING["developer_policy"] = "untrusted"  # type: ignore[index]

    def test_developer_policy_ceiling_is_privileged(self) -> None:
        assert AUTHORITY_TRUST_CEILING["developer_policy"] == "privileged"

    def test_system_config_ceiling_is_privileged(self) -> None:
        assert AUTHORITY_TRUST_CEILING["system_config"] == "privileged"

    def test_user_input_ceiling_is_trusted(self) -> None:
        assert AUTHORITY_TRUST_CEILING["user_input"] == "trusted"

    def test_approved_override_ceiling_is_trusted(self) -> None:
        assert AUTHORITY_TRUST_CEILING["approved_override"] == "trusted"

    def test_tool_output_ceiling_is_provisional(self) -> None:
        assert AUTHORITY_TRUST_CEILING["tool_output"] == "provisional"

    def test_retrieved_content_ceiling_is_provisional(self) -> None:
        assert AUTHORITY_TRUST_CEILING["retrieved_content"] == "provisional"

    def test_memory_content_ceiling_is_provisional(self) -> None:
        assert AUTHORITY_TRUST_CEILING["memory_content"] == "provisional"

    def test_agent_generated_ceiling_is_provisional(self) -> None:
        assert AUTHORITY_TRUST_CEILING["agent_generated"] == "provisional"

    def test_external_message_ceiling_is_untrusted(self) -> None:
        assert AUTHORITY_TRUST_CEILING["external_message"] == "untrusted"

    def test_unknown_ceiling_is_untrusted(self) -> None:
        assert AUTHORITY_TRUST_CEILING["unknown"] == "untrusted"

    def test_all_sources_have_ceiling(self) -> None:
        for src in AuthoritySource:
            assert src.value in AUTHORITY_TRUST_CEILING, f"Missing ceiling for {src}"


# ---------------------------------------------------------------------------
# AuthorityClaim defaults and immutability
# ---------------------------------------------------------------------------


class TestAuthorityClaimDefaults:
    def test_default_source_is_unknown(self) -> None:
        claim = AuthorityClaim()
        assert claim.source is AuthoritySource.UNKNOWN

    def test_default_effective_trust_is_untrusted(self) -> None:
        claim = AuthorityClaim()
        assert claim.effective_trust_level == "untrusted"

    def test_default_chain_is_empty_tuple(self) -> None:
        claim = AuthorityClaim()
        assert claim.chain == ()

    def test_default_approval_id_is_none(self) -> None:
        claim = AuthorityClaim()
        assert claim.approval_id is None

    def test_claim_is_frozen(self) -> None:
        claim = AuthorityClaim(source=AuthoritySource.USER_INPUT)
        with pytest.raises((AttributeError, TypeError)):
            claim.source = AuthoritySource.DEVELOPER_POLICY  # type: ignore[misc]

    def test_metadata_is_immutable(self) -> None:
        claim = AuthorityClaim(metadata={"key": "val"})
        with pytest.raises(TypeError):
            claim.metadata["injected"] = "evil"  # type: ignore[index]

    def test_list_chain_coerced_to_tuple(self) -> None:
        # JSON round-trip safety: list must be stored as tuple
        claim = AuthorityClaim(chain=["a", "b"])  # type: ignore[arg-type]
        assert isinstance(claim.chain, tuple)
        assert claim.chain == ("a", "b")

    def test_created_at_is_float(self) -> None:
        before = time.monotonic()
        claim = AuthorityClaim()
        after = time.monotonic()
        assert before <= claim.created_at <= after


# ---------------------------------------------------------------------------
# effective_trust_level -- ceiling enforcement
# ---------------------------------------------------------------------------


class TestEffectiveTrustLevel:
    def test_developer_policy_no_asserted_returns_privileged(self) -> None:
        claim = _claim(AuthoritySource.DEVELOPER_POLICY)
        assert claim.effective_trust_level == "privileged"

    def test_user_input_no_asserted_returns_trusted(self) -> None:
        claim = _claim(AuthoritySource.USER_INPUT)
        assert claim.effective_trust_level == "trusted"

    def test_tool_output_no_asserted_returns_provisional(self) -> None:
        claim = _claim(AuthoritySource.TOOL_OUTPUT)
        assert claim.effective_trust_level == "provisional"

    def test_external_message_no_asserted_returns_untrusted(self) -> None:
        claim = _claim(AuthoritySource.EXTERNAL_MESSAGE)
        assert claim.effective_trust_level == "untrusted"

    def test_tool_output_asserted_above_ceiling_is_capped(self) -> None:
        # tool_output ceiling = provisional; claiming "privileged" must be capped
        claim = _claim(AuthoritySource.TOOL_OUTPUT, asserted="privileged")
        assert claim.effective_trust_level == "provisional"

    def test_tool_output_asserted_above_ceiling_trusted_is_capped(self) -> None:
        claim = _claim(AuthoritySource.TOOL_OUTPUT, asserted="trusted")
        assert claim.effective_trust_level == "provisional"

    def test_user_input_asserted_below_ceiling_is_respected(self) -> None:
        # user_input ceiling = trusted; claiming "untrusted" should be kept
        claim = _claim(AuthoritySource.USER_INPUT, asserted="untrusted")
        assert claim.effective_trust_level == "untrusted"

    def test_user_input_asserted_at_ceiling_is_kept(self) -> None:
        claim = _claim(AuthoritySource.USER_INPUT, asserted="trusted")
        assert claim.effective_trust_level == "trusted"

    def test_user_input_asserted_above_ceiling_privileged_is_capped(self) -> None:
        claim = _claim(AuthoritySource.USER_INPUT, asserted="privileged")
        assert claim.effective_trust_level == "trusted"

    def test_developer_policy_asserted_below_ceiling(self) -> None:
        claim = _claim(AuthoritySource.DEVELOPER_POLICY, asserted="provisional")
        assert claim.effective_trust_level == "provisional"

    def test_unknown_authority_is_always_untrusted(self) -> None:
        # Regardless of asserted trust, UNKNOWN is capped at untrusted
        claim = _claim(AuthoritySource.UNKNOWN, asserted="privileged")
        assert claim.effective_trust_level == "untrusted"

    def test_external_message_capped_at_untrusted(self) -> None:
        claim = _claim(AuthoritySource.EXTERNAL_MESSAGE, asserted="trusted")
        assert claim.effective_trust_level == "untrusted"

    def test_retrieved_content_capped_at_provisional(self) -> None:
        claim = _claim(AuthoritySource.RETRIEVED_CONTENT, asserted="privileged")
        assert claim.effective_trust_level == "provisional"

    def test_agent_generated_capped_at_provisional(self) -> None:
        claim = _claim(AuthoritySource.AGENT_GENERATED, asserted="privileged")
        assert claim.effective_trust_level == "provisional"

    def test_memory_content_capped_at_provisional(self) -> None:
        claim = _claim(AuthoritySource.MEMORY_CONTENT, asserted="trusted")
        assert claim.effective_trust_level == "provisional"

    def test_effective_trust_never_exceeds_ceiling_for_all_sources(self) -> None:
        """No source can exceed its ceiling even with asserted=privileged."""
        from veronica_core.memory.types import TRUST_RANK

        for src in AuthoritySource:
            claim = AuthorityClaim(source=src, asserted_trust="privileged")
            ceiling = AUTHORITY_TRUST_CEILING[src.value]
            eff_rank = TRUST_RANK.get(claim.effective_trust_level, 0)
            ceil_rank = TRUST_RANK.get(ceiling, 0)
            assert eff_rank <= ceil_rank, (
                f"{src.value}: effective={claim.effective_trust_level} "
                f"exceeds ceiling={ceiling}"
            )


# ---------------------------------------------------------------------------
# trust_rank numeric comparison
# ---------------------------------------------------------------------------


class TestTrustRank:
    def test_developer_policy_has_rank_3(self) -> None:
        claim = _claim(AuthoritySource.DEVELOPER_POLICY)
        assert claim.trust_rank == 3

    def test_user_input_has_rank_2(self) -> None:
        claim = _claim(AuthoritySource.USER_INPUT)
        assert claim.trust_rank == 2

    def test_tool_output_has_rank_1(self) -> None:
        claim = _claim(AuthoritySource.TOOL_OUTPUT)
        assert claim.trust_rank == 1

    def test_unknown_has_rank_0(self) -> None:
        claim = _claim(AuthoritySource.UNKNOWN)
        assert claim.trust_rank == 0

    def test_external_message_has_rank_0(self) -> None:
        claim = _claim(AuthoritySource.EXTERNAL_MESSAGE)
        assert claim.trust_rank == 0

    def test_rank_ordering_respected(self) -> None:
        priv = _claim(AuthoritySource.DEVELOPER_POLICY)
        trusted = _claim(AuthoritySource.USER_INPUT)
        prov = _claim(AuthoritySource.TOOL_OUTPUT)
        untr = _claim(AuthoritySource.UNKNOWN)
        assert priv.trust_rank > trusted.trust_rank > prov.trust_rank > untr.trust_rank


# ---------------------------------------------------------------------------
# derives()
# ---------------------------------------------------------------------------


class TestDerives:
    def test_derives_sets_parent_source(self) -> None:
        parent = _claim(AuthoritySource.USER_INPUT)
        child = parent.derives(AuthoritySource.AGENT_GENERATED)
        assert child.parent_source is AuthoritySource.USER_INPUT

    def test_derives_grows_chain(self) -> None:
        parent = AuthorityClaim(source=AuthoritySource.USER_INPUT, chain=("root",))
        child = parent.derives(AuthoritySource.AGENT_GENERATED)
        assert "user_input" in child.chain
        assert len(child.chain) == len(parent.chain) + 1

    def test_derives_does_not_escalate_trust(self) -> None:
        # tool_output (provisional) cannot produce a child with higher trust
        parent = _claim(AuthoritySource.TOOL_OUTPUT)
        child = parent.derives(AuthoritySource.AGENT_GENERATED)
        assert child.trust_rank <= parent.trust_rank

    def test_derives_from_user_input_cannot_escalate_to_privileged(self) -> None:
        parent = _claim(AuthoritySource.USER_INPUT)
        child = parent.derives(AuthoritySource.DEVELOPER_POLICY)
        # The child's effective trust is constrained by BOTH its ceiling
        # and the inherited asserted_trust from parent (trusted).
        assert child.effective_trust_level in ("trusted", "provisional", "untrusted")

    def test_derives_preserves_chain_tuple_type(self) -> None:
        parent = _claim(AuthoritySource.USER_INPUT)
        child = parent.derives(AuthoritySource.TOOL_OUTPUT)
        assert isinstance(child.chain, tuple)

    def test_derives_kwargs_forwarded(self) -> None:
        parent = _claim(AuthoritySource.USER_INPUT)
        child = parent.derives(
            AuthoritySource.AGENT_GENERATED, metadata={"step": "reasoning"}
        )
        assert child.metadata["step"] == "reasoning"

    def test_double_derives_chain_grows(self) -> None:
        root = AuthorityClaim(source=AuthoritySource.USER_INPUT)
        mid = root.derives(AuthoritySource.AGENT_GENERATED)
        leaf = mid.derives(AuthoritySource.TOOL_OUTPUT)
        assert len(leaf.chain) == len(root.chain) + 2

    def test_derives_does_not_mutate_parent(self) -> None:
        parent = _claim(AuthoritySource.USER_INPUT)
        original_chain = parent.chain
        parent.derives(AuthoritySource.TOOL_OUTPUT)
        assert parent.chain == original_chain


# ---------------------------------------------------------------------------
# with_approval()
# ---------------------------------------------------------------------------


class TestWithApproval:
    def test_with_approval_sets_source_to_approved_override(self) -> None:
        claim = _claim(AuthoritySource.TOOL_OUTPUT)
        approved = claim.with_approval("approval-abc-123")
        assert approved.source is AuthoritySource.APPROVED_OVERRIDE

    def test_with_approval_stores_approval_id(self) -> None:
        claim = _claim(AuthoritySource.AGENT_GENERATED)
        approved = claim.with_approval("approval-xyz")
        assert approved.approval_id == "approval-xyz"

    def test_with_approval_elevates_trust_to_trusted(self) -> None:
        # Starting from tool_output (provisional), approval grants up to trusted
        claim = _claim(AuthoritySource.TOOL_OUTPUT)
        approved = claim.with_approval("approval-1")
        assert approved.effective_trust_level == "trusted"

    def test_with_approval_preserves_parent_source(self) -> None:
        claim = _claim(AuthoritySource.EXTERNAL_MESSAGE)
        approved = claim.with_approval("approval-2")
        assert approved.parent_source is AuthoritySource.EXTERNAL_MESSAGE

    def test_with_approval_grows_chain(self) -> None:
        claim = _claim(AuthoritySource.AGENT_GENERATED)
        approved = claim.with_approval("approval-3")
        assert "agent_generated" in approved.chain

    def test_with_approval_is_immutable(self) -> None:
        claim = _claim(AuthoritySource.TOOL_OUTPUT)
        approved = claim.with_approval("approval-4")
        with pytest.raises((AttributeError, TypeError)):
            approved.approval_id = "tampered"  # type: ignore[misc]

    def test_approval_cannot_exceed_trusted_ceiling(self) -> None:
        # Even after approval, APPROVED_OVERRIDE ceiling is "trusted" (rank 2)
        claim = _claim(AuthoritySource.EXTERNAL_MESSAGE)
        approved = claim.with_approval("ap-5")
        assert approved.trust_rank <= 2  # trusted = rank 2


# ---------------------------------------------------------------------------
# denied_reason
# ---------------------------------------------------------------------------


class TestDeniedReason:
    def test_denied_reason_contains_source(self) -> None:
        claim = _claim(AuthoritySource.TOOL_OUTPUT)
        reason = claim.denied_reason
        assert "tool_output" in reason

    def test_denied_reason_contains_effective_trust(self) -> None:
        claim = _claim(AuthoritySource.TOOL_OUTPUT)
        reason = claim.denied_reason
        assert "provisional" in reason

    def test_denied_reason_is_nonempty_string(self) -> None:
        claim = AuthorityClaim()
        assert isinstance(claim.denied_reason, str)
        assert len(claim.denied_reason) > 0

    def test_denied_reason_contains_chain_info(self) -> None:
        claim = AuthorityClaim(
            source=AuthoritySource.AGENT_GENERATED, chain=("user-abc", "agent-1")
        )
        reason = claim.denied_reason
        # chain is present somewhere in the reason
        assert "user-abc" in reason or "chain" in reason


# ---------------------------------------------------------------------------
# UNKNOWN_AUTHORITY
# ---------------------------------------------------------------------------


class TestUnknownAuthority:
    def test_unknown_authority_source_is_unknown(self) -> None:
        assert UNKNOWN_AUTHORITY.source is AuthoritySource.UNKNOWN

    def test_unknown_authority_is_untrusted(self) -> None:
        assert UNKNOWN_AUTHORITY.effective_trust_level == "untrusted"

    def test_unknown_authority_trust_rank_is_zero(self) -> None:
        assert UNKNOWN_AUTHORITY.trust_rank == 0

    def test_unknown_authority_is_frozen(self) -> None:
        with pytest.raises((AttributeError, TypeError)):
            UNKNOWN_AUTHORITY.source = AuthoritySource.DEVELOPER_POLICY  # type: ignore[misc]


# ---------------------------------------------------------------------------
# is_low_authority / is_policy_authority helpers
# ---------------------------------------------------------------------------


class TestIsLowAuthority:
    """Tests for the is_low_authority() helper."""

    @pytest.mark.parametrize(
        "source",
        [
            AuthoritySource.TOOL_OUTPUT,
            AuthoritySource.RETRIEVED_CONTENT,
            AuthoritySource.MEMORY_CONTENT,
            AuthoritySource.AGENT_GENERATED,
            AuthoritySource.EXTERNAL_MESSAGE,
            AuthoritySource.UNKNOWN,
        ],
    )
    def test_low_authority_sources(self, source: AuthoritySource) -> None:
        claim = _claim(source)
        assert is_low_authority(claim)

    @pytest.mark.parametrize(
        "source",
        [
            AuthoritySource.DEVELOPER_POLICY,
            AuthoritySource.SYSTEM_CONFIG,
            AuthoritySource.USER_INPUT,
            AuthoritySource.APPROVED_OVERRIDE,
        ],
    )
    def test_high_authority_sources(self, source: AuthoritySource) -> None:
        claim = _claim(source)
        assert not is_low_authority(claim)

    def test_non_claim_returns_false(self) -> None:
        assert not is_low_authority(None)
        assert not is_low_authority("not a claim")
        assert not is_low_authority(42)

    def test_asserted_trust_does_not_escalate(self) -> None:
        """tool_output claiming 'privileged' is still low (capped to provisional)."""
        claim = _claim(AuthoritySource.TOOL_OUTPUT, asserted="privileged")
        assert is_low_authority(claim)


class TestIsPolicyAuthority:
    """Tests for the is_policy_authority() helper."""

    def test_developer_policy(self) -> None:
        assert is_policy_authority(_claim(AuthoritySource.DEVELOPER_POLICY))

    def test_system_config(self) -> None:
        assert is_policy_authority(_claim(AuthoritySource.SYSTEM_CONFIG))

    @pytest.mark.parametrize(
        "source",
        [
            AuthoritySource.USER_INPUT,
            AuthoritySource.TOOL_OUTPUT,
            AuthoritySource.AGENT_GENERATED,
            AuthoritySource.APPROVED_OVERRIDE,
            AuthoritySource.UNKNOWN,
        ],
    )
    def test_non_policy_sources(self, source: AuthoritySource) -> None:
        assert not is_policy_authority(_claim(source))

    def test_non_claim_returns_false(self) -> None:
        assert not is_policy_authority(None)
        assert not is_policy_authority("developer_policy")
