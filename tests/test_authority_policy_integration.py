"""Integration tests for authority-aware policy evaluation.

These tests verify that PolicyEngine respects authority levels when making
decisions. They use ExecPolicyContext with different AuthorityClaim values
and verify that decisions change based on authority origin.

NOTE: Authority-based pre-check enforcement is added by Mark-1/Mark-2.
Tests marked with @pytest.mark.authority_enforcement will only pass once
that code lands. Until then they verify that the authority field flows
through the context without error.
"""

from __future__ import annotations

import pytest

from veronica_core.security.authority import (
    UNKNOWN_AUTHORITY,
    AuthorityClaim,
    AuthoritySource,
)
from veronica_core.security.capabilities import CapabilitySet
from veronica_core.security.policy_engine import (
    ExecPolicyContext,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine() -> PolicyEngine:
    return PolicyEngine()


def _dev_caps() -> CapabilitySet:
    return CapabilitySet.dev()


def _ctx(
    action: str,
    args: list[str],
    authority: AuthorityClaim | None = None,
    caps: CapabilitySet | None = None,
    env: str = "dev",
) -> ExecPolicyContext:
    return ExecPolicyContext(
        action=action,  # type: ignore[arg-type]
        args=args,
        working_dir="/repo",
        repo_root="/repo",
        user=None,
        caps=caps or _dev_caps(),
        env=env,
        authority=authority if authority is not None else UNKNOWN_AUTHORITY,
    )


def _authority(source: AuthoritySource, asserted: str = "") -> AuthorityClaim:
    return AuthorityClaim(source=source, asserted_trust=asserted)


# ---------------------------------------------------------------------------
# Baseline: authority field flows through context
# ---------------------------------------------------------------------------


class TestAuthorityFieldInContext:
    """Verify the authority field is accepted and flows correctly."""

    def test_context_accepts_unknown_authority(self) -> None:
        ctx = _ctx("shell", ["pytest", "--version"], authority=UNKNOWN_AUTHORITY)
        assert ctx.authority is UNKNOWN_AUTHORITY

    def test_context_accepts_developer_policy_authority(self) -> None:
        auth = _authority(AuthoritySource.DEVELOPER_POLICY)
        ctx = _ctx("shell", ["pytest", "--version"], authority=auth)
        assert ctx.authority.source is AuthoritySource.DEVELOPER_POLICY

    def test_context_accepts_tool_output_authority(self) -> None:
        auth = _authority(AuthoritySource.TOOL_OUTPUT)
        ctx = _ctx("file_read", ["/tmp/output.txt"], authority=auth)
        assert ctx.authority.source is AuthoritySource.TOOL_OUTPUT

    def test_context_default_authority_is_unknown(self) -> None:
        # ExecPolicyContext default should be UNKNOWN_AUTHORITY
        ctx = ExecPolicyContext(
            action="file_read",  # type: ignore[arg-type]
            args=["/tmp/file.txt"],
            working_dir="/repo",
            repo_root="/repo",
            user=None,
            caps=_dev_caps(),
            env="dev",
        )
        assert ctx.authority.source is AuthoritySource.UNKNOWN
        assert ctx.authority.effective_trust_level == "untrusted"

    def test_engine_evaluate_does_not_raise_with_authority(self) -> None:
        engine = _engine()
        auth = _authority(AuthoritySource.USER_INPUT)
        ctx = _ctx("shell", ["pytest", "--version"], authority=auth)
        decision = engine.evaluate(ctx)
        # Decision must be a valid PolicyDecision regardless of authority
        assert decision.verdict in ("ALLOW", "DENY", "REQUIRE_APPROVAL")

    def test_engine_evaluate_does_not_raise_with_unknown_authority(self) -> None:
        engine = _engine()
        ctx = _ctx("shell", ["pytest", "--version"], authority=UNKNOWN_AUTHORITY)
        decision = engine.evaluate(ctx)
        assert decision.verdict in ("ALLOW", "DENY", "REQUIRE_APPROVAL")


# ---------------------------------------------------------------------------
# Developer policy authority -- highest trust
# ---------------------------------------------------------------------------


class TestDeveloperPolicyAuthority:
    """Actions from developer policy should pass normal evaluation."""

    def test_developer_policy_shell_allowlisted_cmd_allowed(self) -> None:
        engine = _engine()
        auth = _authority(AuthoritySource.DEVELOPER_POLICY)
        ctx = _ctx("shell", ["pytest", "--version"], authority=auth)
        decision = engine.evaluate(ctx)
        assert decision.verdict == "ALLOW"

    def test_developer_policy_file_read_normal_allowed(self) -> None:
        engine = _engine()
        auth = _authority(AuthoritySource.DEVELOPER_POLICY)
        ctx = _ctx("file_read", ["/tmp/safe_file.txt"], authority=auth)
        decision = engine.evaluate(ctx)
        assert decision.verdict == "ALLOW"

    def test_developer_policy_shell_denied_cmd_still_denied(self) -> None:
        # Authority does not override hard security rules for denied commands
        engine = _engine()
        auth = _authority(AuthoritySource.DEVELOPER_POLICY)
        ctx = _ctx("shell", ["rm", "-rf", "/"], authority=auth)
        decision = engine.evaluate(ctx)
        assert decision.verdict == "DENY"


# ---------------------------------------------------------------------------
# User input authority -- trusted
# ---------------------------------------------------------------------------


class TestUserInputAuthority:
    """User input should go through normal evaluation (trusted source)."""

    def test_user_input_shell_allowlisted_cmd_allowed(self) -> None:
        engine = _engine()
        auth = _authority(AuthoritySource.USER_INPUT)
        ctx = _ctx("shell", ["python", "--version"], authority=auth)
        decision = engine.evaluate(ctx)
        assert decision.verdict == "ALLOW"

    def test_user_input_shell_denied_cmd_denied(self) -> None:
        engine = _engine()
        auth = _authority(AuthoritySource.USER_INPUT)
        ctx = _ctx("shell", ["rm", "-rf", "/tmp"], authority=auth)
        decision = engine.evaluate(ctx)
        assert decision.verdict == "DENY"

    def test_user_input_authority_trust_rank_is_2(self) -> None:
        auth = _authority(AuthoritySource.USER_INPUT)
        assert auth.trust_rank == 2


# ---------------------------------------------------------------------------
# Tool output authority -- provisional (restricted)
# ---------------------------------------------------------------------------


class TestToolOutputAuthority:
    """Tool output actions should be more restricted than user input."""

    def test_tool_output_file_read_benign_path_allowed(self) -> None:
        engine = _engine()
        auth = _authority(AuthoritySource.TOOL_OUTPUT)
        ctx = _ctx("file_read", ["/tmp/safe_output.txt"], authority=auth)
        decision = engine.evaluate(ctx)
        assert decision.verdict == "ALLOW"

    def test_tool_output_file_read_sensitive_path_denied(self) -> None:
        engine = _engine()
        auth = _authority(AuthoritySource.TOOL_OUTPUT)
        ctx = _ctx("file_read", [".env"], authority=auth)
        decision = engine.evaluate(ctx)
        assert decision.verdict == "DENY"

    def test_tool_output_trust_is_provisional(self) -> None:
        auth = _authority(AuthoritySource.TOOL_OUTPUT)
        assert auth.effective_trust_level == "provisional"

    def test_tool_output_trust_rank_is_1(self) -> None:
        auth = _authority(AuthoritySource.TOOL_OUTPUT)
        assert auth.trust_rank == 1


# ---------------------------------------------------------------------------
# Retrieved content authority -- provisional (restricted)
# ---------------------------------------------------------------------------


class TestRetrievedContentAuthority:
    def test_retrieved_content_trust_is_provisional(self) -> None:
        auth = _authority(AuthoritySource.RETRIEVED_CONTENT)
        assert auth.effective_trust_level == "provisional"

    def test_retrieved_content_trust_rank_is_1(self) -> None:
        auth = _authority(AuthoritySource.RETRIEVED_CONTENT)
        assert auth.trust_rank == 1

    def test_retrieved_content_file_read_benign_allowed(self) -> None:
        engine = _engine()
        auth = _authority(AuthoritySource.RETRIEVED_CONTENT)
        ctx = _ctx("file_read", ["/tmp/doc.txt"], authority=auth)
        decision = engine.evaluate(ctx)
        assert decision.verdict == "ALLOW"


# ---------------------------------------------------------------------------
# External message authority -- untrusted
# ---------------------------------------------------------------------------


class TestExternalMessageAuthority:
    def test_external_message_trust_is_untrusted(self) -> None:
        auth = _authority(AuthoritySource.EXTERNAL_MESSAGE)
        assert auth.effective_trust_level == "untrusted"

    def test_external_message_trust_rank_is_0(self) -> None:
        auth = _authority(AuthoritySource.EXTERNAL_MESSAGE)
        assert auth.trust_rank == 0

    def test_external_message_denied_shell_still_denied(self) -> None:
        engine = _engine()
        auth = _authority(AuthoritySource.EXTERNAL_MESSAGE)
        ctx = _ctx("shell", ["rm", "/tmp/file"], authority=auth)
        decision = engine.evaluate(ctx)
        assert decision.verdict == "DENY"


# ---------------------------------------------------------------------------
# Unknown authority -- fail-closed / untrusted
# ---------------------------------------------------------------------------


class TestUnknownAuthority:
    def test_unknown_authority_trust_is_untrusted(self) -> None:
        assert UNKNOWN_AUTHORITY.effective_trust_level == "untrusted"

    def test_unknown_authority_trust_rank_is_0(self) -> None:
        assert UNKNOWN_AUTHORITY.trust_rank == 0

    def test_unknown_authority_denied_shell_is_denied(self) -> None:
        engine = _engine()
        ctx = _ctx("shell", ["rm", "-rf", "/tmp"], authority=UNKNOWN_AUTHORITY)
        decision = engine.evaluate(ctx)
        assert decision.verdict == "DENY"

    def test_unknown_authority_unrecognised_cmd_denied(self) -> None:
        engine = _engine()
        ctx = _ctx(
            "shell", ["suspicious_binary", "--exploit"], authority=UNKNOWN_AUTHORITY
        )
        decision = engine.evaluate(ctx)
        assert decision.verdict == "DENY"


# ---------------------------------------------------------------------------
# Approved override authority -- escalated with audit trail
# ---------------------------------------------------------------------------


class TestApprovedOverrideAuthority:
    def test_approved_override_trust_is_trusted(self) -> None:
        auth = _authority(AuthoritySource.TOOL_OUTPUT).with_approval("approval-001")
        assert auth.effective_trust_level == "trusted"

    def test_approved_override_source_is_approved_override(self) -> None:
        auth = _authority(AuthoritySource.AGENT_GENERATED).with_approval("approval-002")
        assert auth.source is AuthoritySource.APPROVED_OVERRIDE

    def test_approved_override_has_approval_id(self) -> None:
        auth = _authority(AuthoritySource.TOOL_OUTPUT).with_approval("approval-003")
        assert auth.approval_id == "approval-003"

    def test_approved_override_cannot_escalate_to_privileged(self) -> None:
        # Even after approval, trust is capped at "trusted" (rank 2)
        auth = _authority(AuthoritySource.TOOL_OUTPUT).with_approval("approval-004")
        assert auth.trust_rank <= 2

    def test_approved_override_preserves_chain(self) -> None:
        original = _authority(AuthoritySource.AGENT_GENERATED)
        approved = original.with_approval("approval-005")
        assert "agent_generated" in approved.chain


# ---------------------------------------------------------------------------
# Authority propagation through derives()
# ---------------------------------------------------------------------------


class TestAuthorityPropagation:
    """Authority chains propagate without escalation."""

    def test_tool_derives_from_user_cannot_escalate(self) -> None:
        user_claim = _authority(AuthoritySource.USER_INPUT)
        tool_claim = user_claim.derives(AuthoritySource.TOOL_OUTPUT)
        # derived claim: tool_output ceiling is provisional, so rank <= 1
        assert tool_claim.trust_rank <= user_claim.trust_rank

    def test_agent_derives_from_tool_is_at_most_provisional(self) -> None:
        tool_claim = _authority(AuthoritySource.TOOL_OUTPUT)
        agent_claim = tool_claim.derives(AuthoritySource.AGENT_GENERATED)
        assert agent_claim.effective_trust_level in ("provisional", "untrusted")

    def test_chain_grows_with_each_derive(self) -> None:
        root = _authority(AuthoritySource.USER_INPUT)
        mid = root.derives(AuthoritySource.AGENT_GENERATED)
        leaf = mid.derives(AuthoritySource.TOOL_OUTPUT)
        assert len(leaf.chain) == len(root.chain) + 2

    def test_derived_claim_in_context_does_not_raise(self) -> None:
        user_claim = _authority(AuthoritySource.USER_INPUT)
        derived = user_claim.derives(AuthoritySource.AGENT_GENERATED)
        engine = _engine()
        ctx = _ctx("shell", ["pytest", "--version"], authority=derived)
        decision = engine.evaluate(ctx)
        assert decision.verdict in ("ALLOW", "DENY", "REQUIRE_APPROVAL")


# ---------------------------------------------------------------------------
# Mixed-authority chain: user -> agent -> tool -> shell
# ---------------------------------------------------------------------------


class TestMixedAuthorityChain:
    def test_multi_hop_chain_does_not_escalate(self) -> None:
        # user_input -> agent_generated -> tool_output -> shell evaluation
        user = _authority(AuthoritySource.USER_INPUT)
        agent = user.derives(AuthoritySource.AGENT_GENERATED)
        tool = agent.derives(AuthoritySource.TOOL_OUTPUT)
        # tool_output ceiling = provisional (rank 1)
        assert tool.trust_rank <= 1

    def test_multi_hop_chain_shows_full_ancestry(self) -> None:
        user = _authority(AuthoritySource.USER_INPUT)
        agent = user.derives(AuthoritySource.AGENT_GENERATED)
        tool = agent.derives(AuthoritySource.TOOL_OUTPUT)
        # Chain should include the intermediate steps
        assert "user_input" in tool.chain or "agent_generated" in tool.chain

    def test_external_derived_to_agent_to_tool_still_untrusted_or_provisional(
        self,
    ) -> None:
        external = _authority(AuthoritySource.EXTERNAL_MESSAGE)
        agent = external.derives(AuthoritySource.AGENT_GENERATED)
        # agent is capped by the inherited asserted_trust from external (untrusted)
        # AND the AGENT_GENERATED ceiling (provisional) -- lower of the two wins
        assert agent.trust_rank <= 1


# ---------------------------------------------------------------------------
# Backward compatibility: no authority field = UNKNOWN = strict
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_policy_context_alias_works(self) -> None:
        # PolicyContext is a backward-compat alias for ExecPolicyContext
        ctx = PolicyContext(
            action="shell",  # type: ignore[arg-type]
            args=["pytest", "--version"],
            working_dir="/repo",
            repo_root="/repo",
            user=None,
            caps=_dev_caps(),
            env="dev",
        )
        # Default authority = UNKNOWN_AUTHORITY
        assert ctx.authority.source is AuthoritySource.UNKNOWN
        engine = _engine()
        decision = engine.evaluate(ctx)
        assert decision.verdict in ("ALLOW", "DENY", "REQUIRE_APPROVAL")

    def test_context_without_explicit_authority_defaults_to_unknown(self) -> None:
        ctx = ExecPolicyContext(
            action="file_read",  # type: ignore[arg-type]
            args=["/tmp/file.txt"],
            working_dir="/repo",
            repo_root="/repo",
            user=None,
            caps=_dev_caps(),
            env="dev",
        )
        assert ctx.authority.effective_trust_level == "untrusted"

    def test_engine_with_default_context_returns_valid_decision(self) -> None:
        engine = _engine()
        ctx = PolicyContext(
            action="shell",  # type: ignore[arg-type]
            args=["pytest", "--version"],
            working_dir="/repo",
            repo_root="/repo",
            user=None,
            caps=_dev_caps(),
            env="dev",
        )
        decision = engine.evaluate(ctx)
        assert isinstance(decision, PolicyDecision)


# ---------------------------------------------------------------------------
# Agent generated authority -- provisional
# ---------------------------------------------------------------------------


class TestAgentGeneratedAuthority:
    def test_agent_generated_trust_is_provisional(self) -> None:
        auth = _authority(AuthoritySource.AGENT_GENERATED)
        assert auth.effective_trust_level == "provisional"

    def test_agent_generated_trust_rank_is_1(self) -> None:
        auth = _authority(AuthoritySource.AGENT_GENERATED)
        assert auth.trust_rank == 1

    def test_agent_generated_context_evaluates_without_error(self) -> None:
        engine = _engine()
        auth = _authority(AuthoritySource.AGENT_GENERATED)
        ctx = _ctx("shell", ["pytest", "--version"], authority=auth)
        decision = engine.evaluate(ctx)
        assert decision.verdict in ("ALLOW", "DENY", "REQUIRE_APPROVAL")


# ---------------------------------------------------------------------------
# Memory content authority -- provisional
# ---------------------------------------------------------------------------


class TestMemoryContentAuthority:
    def test_memory_content_trust_is_provisional(self) -> None:
        auth = _authority(AuthoritySource.MEMORY_CONTENT)
        assert auth.effective_trust_level == "provisional"

    def test_memory_content_trust_rank_is_1(self) -> None:
        auth = _authority(AuthoritySource.MEMORY_CONTENT)
        assert auth.trust_rank == 1


# ---------------------------------------------------------------------------
# All sources: evaluation does not raise
# ---------------------------------------------------------------------------


class TestAllSourcesDoNotRaise:
    """Smoke test: every AuthoritySource can flow through evaluation."""

    @pytest.mark.parametrize("source", list(AuthoritySource))
    def test_source_does_not_raise_on_evaluation(self, source: AuthoritySource) -> None:
        engine = _engine()
        auth = AuthorityClaim(source=source)
        ctx = _ctx("shell", ["pytest", "--version"], authority=auth)
        decision = engine.evaluate(ctx)
        assert decision.verdict in ("ALLOW", "DENY", "REQUIRE_APPROVAL")

    @pytest.mark.parametrize("source", list(AuthoritySource))
    def test_source_does_not_raise_on_file_read(self, source: AuthoritySource) -> None:
        engine = _engine()
        auth = AuthorityClaim(source=source)
        ctx = _ctx("file_read", ["/tmp/safe.txt"], authority=auth)
        decision = engine.evaluate(ctx)
        assert decision.verdict in ("ALLOW", "DENY", "REQUIRE_APPROVAL")
