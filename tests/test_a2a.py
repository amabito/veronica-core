"""Happy-path tests for the A2A trust boundary module."""

from __future__ import annotations

import pytest

from veronica_core.a2a import (
    AgentIdentity,
    TrustBasedPolicyRouter,
    TrustEscalationTracker,
    TrustLevel,
    TrustPolicy,
)


class TestTrustLevel:
    def test_enum_values(self) -> None:
        assert TrustLevel.UNTRUSTED.value == "untrusted"
        assert TrustLevel.PROVISIONAL.value == "provisional"
        assert TrustLevel.TRUSTED.value == "trusted"
        assert TrustLevel.PRIVILEGED.value == "privileged"

    def test_str_comparison(self) -> None:
        # TrustLevel inherits from str
        assert TrustLevel.UNTRUSTED == "untrusted"
        assert TrustLevel.PROVISIONAL == "provisional"
        assert TrustLevel.TRUSTED == "trusted"
        assert TrustLevel.PRIVILEGED == "privileged"

    def test_all_members_present(self) -> None:
        members = {level.value for level in TrustLevel}
        assert members == {"untrusted", "provisional", "trusted", "privileged"}


class TestAgentIdentity:
    def test_valid_local_origin(self) -> None:
        agent = AgentIdentity(agent_id="agent-1", origin="local")
        assert agent.agent_id == "agent-1"
        assert agent.origin == "local"
        assert agent.trust_level == TrustLevel.UNTRUSTED

    def test_valid_a2a_origin(self) -> None:
        agent = AgentIdentity(
            agent_id="agent-2", origin="a2a", trust_level=TrustLevel.PROVISIONAL
        )
        assert agent.trust_level == TrustLevel.PROVISIONAL

    def test_valid_mcp_origin(self) -> None:
        agent = AgentIdentity(agent_id="agent-3", origin="mcp")
        assert agent.origin == "mcp"

    def test_frozen_dataclass(self) -> None:
        agent = AgentIdentity(agent_id="agent-1", origin="local")
        with pytest.raises((AttributeError, TypeError)):
            agent.agent_id = "other"  # type: ignore[misc]

    def test_invalid_origin_raises(self) -> None:
        with pytest.raises(ValueError, match="origin"):
            AgentIdentity(agent_id="agent-1", origin="invalid")

    def test_empty_agent_id_raises(self) -> None:
        with pytest.raises(ValueError, match="agent_id"):
            AgentIdentity(agent_id="", origin="local")

    def test_metadata_default_empty(self) -> None:
        agent = AgentIdentity(agent_id="agent-1", origin="local")
        assert agent.metadata == {}

    def test_metadata_custom(self) -> None:
        agent = AgentIdentity(
            agent_id="agent-1", origin="a2a", metadata={"key": "value"}
        )
        assert agent.metadata["key"] == "value"


class TestTrustPolicy:
    def test_default_construction(self) -> None:
        policy = TrustPolicy()
        assert policy.default_trust == TrustLevel.UNTRUSTED
        assert policy.promotion_threshold == 10
        assert policy.allow_promotion_to == TrustLevel.PROVISIONAL

    def test_custom_threshold(self) -> None:
        policy = TrustPolicy(promotion_threshold=5)
        assert policy.promotion_threshold == 5

    def test_zero_threshold_raises(self) -> None:
        with pytest.raises(ValueError, match="promotion_threshold"):
            TrustPolicy(promotion_threshold=0)

    def test_negative_threshold_raises(self) -> None:
        with pytest.raises(ValueError, match="promotion_threshold"):
            TrustPolicy(promotion_threshold=-1)

    def test_privileged_allow_promotion_raises(self) -> None:
        with pytest.raises(ValueError, match="PRIVILEGED"):
            TrustPolicy(allow_promotion_to=TrustLevel.PRIVILEGED)

    def test_allow_promotion_to_trusted(self) -> None:
        policy = TrustPolicy(allow_promotion_to=TrustLevel.TRUSTED)
        assert policy.allow_promotion_to == TrustLevel.TRUSTED


class TestTrustBasedPolicyRouter:
    def test_route_by_trust_level(self) -> None:
        from veronica_core.shield.pipeline import ShieldPipeline

        untrusted_pipeline = ShieldPipeline()
        trusted_pipeline = ShieldPipeline()
        router = TrustBasedPolicyRouter(
            policies={
                TrustLevel.UNTRUSTED: untrusted_pipeline,
                TrustLevel.TRUSTED: trusted_pipeline,
            }
        )
        untrusted_agent = AgentIdentity(
            agent_id="u", origin="a2a", trust_level=TrustLevel.UNTRUSTED
        )
        trusted_agent = AgentIdentity(
            agent_id="t", origin="a2a", trust_level=TrustLevel.TRUSTED
        )

        assert router.route(untrusted_agent) is untrusted_pipeline
        assert router.route(trusted_agent) is trusted_pipeline

    def test_fallback_to_default(self) -> None:
        from veronica_core.shield.pipeline import ShieldPipeline

        default_pipeline = ShieldPipeline()
        router = TrustBasedPolicyRouter(default_policy=default_pipeline)
        agent = AgentIdentity(
            agent_id="x", origin="local", trust_level=TrustLevel.PROVISIONAL
        )
        assert router.route(agent) is default_pipeline

    def test_missing_level_uses_default(self) -> None:
        from veronica_core.shield.pipeline import ShieldPipeline

        default_pipeline = ShieldPipeline()
        router = TrustBasedPolicyRouter(default_policy=default_pipeline)
        agent = AgentIdentity(
            agent_id="x", origin="local", trust_level=TrustLevel.PRIVILEGED
        )
        assert router.route(agent) is default_pipeline

    def test_get_policy_for(self) -> None:
        from veronica_core.shield.pipeline import ShieldPipeline

        p = ShieldPipeline()
        router = TrustBasedPolicyRouter(policies={TrustLevel.TRUSTED: p})
        assert router.get_policy_for(TrustLevel.TRUSTED) is p

    def test_empty_policies_always_returns_default(self) -> None:
        from veronica_core.shield.pipeline import ShieldPipeline

        default_pipeline = ShieldPipeline()
        router = TrustBasedPolicyRouter(policies={}, default_policy=default_pipeline)
        for level in TrustLevel:
            agent = AgentIdentity(agent_id="x", origin="local", trust_level=level)
            assert router.route(agent) is default_pipeline


class TestTrustEscalationTracker:
    def test_unknown_agent_gets_default_trust(self) -> None:
        policy = TrustPolicy(default_trust=TrustLevel.UNTRUSTED)
        tracker = TrustEscalationTracker(policy=policy)
        assert tracker.get_trust_level("unknown-agent") == TrustLevel.UNTRUSTED

    def test_record_success_promotes_after_threshold(self) -> None:
        policy = TrustPolicy(
            promotion_threshold=3, allow_promotion_to=TrustLevel.PROVISIONAL
        )
        tracker = TrustEscalationTracker(policy=policy)

        for _ in range(2):
            level = tracker.record_success("agent-1")
            assert level == TrustLevel.UNTRUSTED

        level = tracker.record_success("agent-1")
        assert level == TrustLevel.PROVISIONAL

    def test_record_failure_demotes_to_untrusted(self) -> None:
        policy = TrustPolicy(
            promotion_threshold=2, allow_promotion_to=TrustLevel.PROVISIONAL
        )
        tracker = TrustEscalationTracker(policy=policy)

        tracker.record_success("agent-1")
        tracker.record_success("agent-1")
        assert tracker.get_trust_level("agent-1") == TrustLevel.PROVISIONAL

        level = tracker.record_failure("agent-1")
        assert level == TrustLevel.UNTRUSTED

    def test_promotion_cap_not_exceeded(self) -> None:
        # allow_promotion_to=PROVISIONAL means auto-promo stops at PROVISIONAL
        policy = TrustPolicy(
            promotion_threshold=3, allow_promotion_to=TrustLevel.PROVISIONAL
        )
        tracker = TrustEscalationTracker(policy=policy)

        # Promote to PROVISIONAL
        for _ in range(3):
            tracker.record_success("agent-1")
        assert tracker.get_trust_level("agent-1") == TrustLevel.PROVISIONAL

        # Keep accumulating; must NOT reach TRUSTED
        for _ in range(10):
            tracker.record_success("agent-1")
        assert tracker.get_trust_level("agent-1") == TrustLevel.PROVISIONAL

    def test_get_stats_snapshot(self) -> None:
        policy = TrustPolicy(promotion_threshold=5)
        tracker = TrustEscalationTracker(policy=policy)

        tracker.record_success("agent-1")
        tracker.record_success("agent-1")
        tracker.record_failure("agent-1")

        stats = tracker.get_stats("agent-1")
        assert stats["success_count"] == 0  # reset on demote
        assert stats["failure_count"] == 1
        assert stats["current_trust"] == TrustLevel.UNTRUSTED.value

    def test_get_stats_unknown_agent(self) -> None:
        policy = TrustPolicy()
        tracker = TrustEscalationTracker(policy=policy)
        stats = tracker.get_stats("nonexistent")
        assert stats["success_count"] == 0
        assert stats["failure_count"] == 0
        assert stats["promoted_at"] is None

    def test_success_count_resets_after_promotion(self) -> None:
        policy = TrustPolicy(
            promotion_threshold=2, allow_promotion_to=TrustLevel.PROVISIONAL
        )
        tracker = TrustEscalationTracker(policy=policy)

        tracker.record_success("agent-1")
        tracker.record_success("agent-1")
        # Promoted; success_count should reset
        stats = tracker.get_stats("agent-1")
        assert stats["success_count"] == 0
        assert stats["current_trust"] == TrustLevel.PROVISIONAL.value
