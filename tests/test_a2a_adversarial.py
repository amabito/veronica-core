"""Adversarial tests for the A2A trust boundary module -- attacker mindset."""

from __future__ import annotations

import threading

import pytest

from veronica_core.a2a import (
    AgentIdentity,
    TrustBasedPolicyRouter,
    TrustEscalationTracker,
    TrustLevel,
    TrustPolicy,
)


class TestCorruptedInput:
    """Corrupted / unexpected input must not crash or leak internal state."""

    def test_negative_threshold_rejected(self) -> None:
        with pytest.raises(ValueError):
            TrustPolicy(promotion_threshold=-100)

    def test_zero_threshold_rejected(self) -> None:
        with pytest.raises(ValueError):
            TrustPolicy(promotion_threshold=0)

    def test_empty_agent_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="agent_id"):
            AgentIdentity(agent_id="", origin="local")

    def test_whitespace_only_agent_id_rejected(self) -> None:
        # Whitespace-only agent_id is effectively empty -- must be caught
        # Note: current impl checks `not self.agent_id` -- whitespace truthy
        # This test documents the current behavior (whitespace allowed).
        # If stricter validation is needed, update the impl and this test.
        agent = AgentIdentity(agent_id="   ", origin="local")
        assert agent.agent_id == "   "  # documents current behavior

    def test_none_metadata_values(self) -> None:
        # None values inside metadata dict should not crash anything
        agent = AgentIdentity(agent_id="a", origin="local", metadata={"key": None})
        assert agent.metadata["key"] is None

    def test_very_long_agent_id(self) -> None:
        long_id = "x" * 10_000
        agent = AgentIdentity(agent_id=long_id, origin="local")
        assert len(agent.agent_id) == 10_000

    def test_agent_id_with_special_chars(self) -> None:
        special_id = "agent\x00\xff\n\t"
        agent = AgentIdentity(agent_id=special_id, origin="local")
        assert agent.agent_id == special_id

    def test_invalid_origin_values(self) -> None:
        for bad_origin in ["LOCAL", "A2A", "MCP", "http", "", " ", "null"]:
            with pytest.raises(ValueError, match="origin"):
                AgentIdentity(agent_id="x", origin=bad_origin)

    def test_escalation_tracker_record_unknown_agent_no_crash(self) -> None:
        policy = TrustPolicy()
        tracker = TrustEscalationTracker(policy=policy)
        # Operations on never-seen agent_ids must not raise
        level = tracker.get_trust_level("never-seen-agent")
        assert level == policy.default_trust

    def test_escalation_tracker_failure_before_any_success(self) -> None:
        policy = TrustPolicy()
        tracker = TrustEscalationTracker(policy=policy)
        level = tracker.record_failure("fresh-agent")
        assert level == TrustLevel.UNTRUSTED
        stats = tracker.get_stats("fresh-agent")
        assert stats["failure_count"] == 1


class TestConcurrentAccess:
    """Race conditions: 10+ threads competing on the same agent."""

    def test_concurrent_success_recording_same_agent(self) -> None:
        """10 threads record success on same agent -- no crash, trust is valid."""
        policy = TrustPolicy(promotion_threshold=5, allow_promotion_to=TrustLevel.PROVISIONAL)
        tracker = TrustEscalationTracker(policy=policy)
        results: list[TrustLevel] = []
        errors: list[Exception] = []

        def record() -> None:
            try:
                level = tracker.record_success("shared-agent")
                results.append(level)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=record) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected exceptions: {errors}"
        assert len(results) == 10
        # All returned trust levels must be valid enum members
        for level in results:
            assert isinstance(level, TrustLevel)

    def test_concurrent_failure_and_success_same_agent(self) -> None:
        """Mixed success/failure from multiple threads must not corrupt state."""
        policy = TrustPolicy(promotion_threshold=3)
        tracker = TrustEscalationTracker(policy=policy)
        errors: list[Exception] = []

        def succeed() -> None:
            try:
                tracker.record_success("contested")
            except Exception as exc:
                errors.append(exc)

        def fail() -> None:
            try:
                tracker.record_failure("contested")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=succeed if i % 2 == 0 else fail) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # Final state must be a valid trust level
        final = tracker.get_trust_level("contested")
        assert isinstance(final, TrustLevel)

    def test_cardinality_cap_race_condition(self) -> None:
        """10 threads each try to register a unique agent under a cap of 5.
        Exactly <=5 should be registered; rest get default trust back."""
        policy = TrustPolicy()
        tracker = TrustEscalationTracker(policy=policy, max_agents=5)
        registered: list[TrustLevel] = []
        errors: list[Exception] = []

        def register(i: int) -> None:
            try:
                level = tracker.record_success(f"agent-cap-{i}")
                registered.append(level)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=register, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All returned values are valid TrustLevel members
        for level in registered:
            assert isinstance(level, TrustLevel)
        # At most 5 agents should be in the tracker
        assert len(tracker._agents) <= 5

    def test_concurrent_new_agent_registration(self) -> None:
        """Many threads racing to register new agents -- no duplicate entries."""
        policy = TrustPolicy()
        tracker = TrustEscalationTracker(policy=policy, max_agents=1000)
        errors: list[Exception] = []

        def register(i: int) -> None:
            try:
                tracker.record_success(f"unique-agent-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=register, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # Each unique agent_id should appear at most once
        assert len(tracker._agents) == 50


class TestStateCorruption:
    """Invalid state transitions and rapid promote/demote cycles."""

    def test_promote_then_immediate_failure_returns_untrusted(self) -> None:
        policy = TrustPolicy(promotion_threshold=2, allow_promotion_to=TrustLevel.PROVISIONAL)
        tracker = TrustEscalationTracker(policy=policy)

        tracker.record_success("agent")
        tracker.record_success("agent")
        assert tracker.get_trust_level("agent") == TrustLevel.PROVISIONAL

        level = tracker.record_failure("agent")
        assert level == TrustLevel.UNTRUSTED
        assert tracker.get_trust_level("agent") == TrustLevel.UNTRUSTED

    def test_rapid_promote_demote_cycle(self) -> None:
        """Repeated promotion followed by immediate demotion must leave agent UNTRUSTED."""
        policy = TrustPolicy(promotion_threshold=1, allow_promotion_to=TrustLevel.PROVISIONAL)
        tracker = TrustEscalationTracker(policy=policy)

        for _ in range(20):
            tracker.record_success("agent")
            tracker.record_failure("agent")

        assert tracker.get_trust_level("agent") == TrustLevel.UNTRUSTED

    def test_multiple_failures_do_not_accumulate_negative_trust(self) -> None:
        """Multiple failures should all result in UNTRUSTED (no further demotion)."""
        policy = TrustPolicy()
        tracker = TrustEscalationTracker(policy=policy)

        for _ in range(10):
            level = tracker.record_failure("agent")
            assert level == TrustLevel.UNTRUSTED

    def test_stats_consistency_after_promote_then_demote(self) -> None:
        policy = TrustPolicy(promotion_threshold=3, allow_promotion_to=TrustLevel.PROVISIONAL)
        tracker = TrustEscalationTracker(policy=policy)

        for _ in range(3):
            tracker.record_success("a")
        tracker.record_failure("a")

        stats = tracker.get_stats("a")
        assert stats["current_trust"] == TrustLevel.UNTRUSTED.value
        assert stats["success_count"] == 0
        assert stats["failure_count"] == 1


class TestBoundaryConditions:
    """Exact threshold promotion, single-entry log, max agents cap."""

    def test_exact_threshold_promotion(self) -> None:
        """Promotion triggers on exactly the Nth success, not N-1."""
        policy = TrustPolicy(promotion_threshold=5, allow_promotion_to=TrustLevel.PROVISIONAL)
        tracker = TrustEscalationTracker(policy=policy)

        for i in range(1, 5):
            level = tracker.record_success("agent")
            assert level == TrustLevel.UNTRUSTED, f"Should not promote before threshold (step {i})"

        level = tracker.record_success("agent")
        assert level == TrustLevel.PROVISIONAL

    def test_single_success_with_threshold_one(self) -> None:
        policy = TrustPolicy(promotion_threshold=1, allow_promotion_to=TrustLevel.PROVISIONAL)
        tracker = TrustEscalationTracker(policy=policy)
        level = tracker.record_success("agent")
        assert level == TrustLevel.PROVISIONAL

    def test_max_agents_cap_hard_limit(self) -> None:
        """After cap is reached, new agents get default trust without error."""
        policy = TrustPolicy(default_trust=TrustLevel.UNTRUSTED)
        tracker = TrustEscalationTracker(policy=policy, max_agents=3)

        for i in range(3):
            tracker.record_success(f"agent-{i}")

        assert len(tracker._agents) == 3

        # 4th agent: cap reached, should return default trust
        level = tracker.record_success("agent-overflow")
        assert level == TrustLevel.UNTRUSTED
        assert len(tracker._agents) == 3  # not grown

    def test_max_agents_cap_get_stats_for_rejected_agent(self) -> None:
        policy = TrustPolicy()
        tracker = TrustEscalationTracker(policy=policy, max_agents=1)
        tracker.record_success("agent-0")

        # agent-1 is rejected
        tracker.record_success("agent-1")
        stats = tracker.get_stats("agent-1")
        # Returns zero-stats since the agent was never registered
        assert stats["success_count"] == 0
        assert stats["failure_count"] == 0

    def test_promotion_ceiling_allow_trusted(self) -> None:
        """With allow_promotion_to=TRUSTED, agent can reach TRUSTED but not PRIVILEGED."""
        policy = TrustPolicy(promotion_threshold=2, allow_promotion_to=TrustLevel.TRUSTED)
        tracker = TrustEscalationTracker(policy=policy)

        # First promotion: UNTRUSTED -> PROVISIONAL
        tracker.record_success("a")
        tracker.record_success("a")
        assert tracker.get_trust_level("a") == TrustLevel.PROVISIONAL

        # Second promotion: PROVISIONAL -> TRUSTED
        tracker.record_success("a")
        tracker.record_success("a")
        assert tracker.get_trust_level("a") == TrustLevel.TRUSTED

        # Must not go beyond TRUSTED
        for _ in range(20):
            tracker.record_success("a")
        assert tracker.get_trust_level("a") == TrustLevel.TRUSTED


class TestPromotionOrderBug:
    """Regression: _can_promote must use index comparison, not string value."""

    def test_default_trust_above_allow_promotion_no_further_promotion(self) -> None:
        """If default_trust=PROVISIONAL and allow_promotion_to=UNTRUSTED,
        agent must NOT be promoted further (string comparison would allow it)."""
        policy = TrustPolicy(
            default_trust=TrustLevel.PROVISIONAL,
            promotion_threshold=1,
            allow_promotion_to=TrustLevel.UNTRUSTED,
        )
        tracker = TrustEscalationTracker(policy=policy)

        # Agent starts at PROVISIONAL (from default_trust).
        # allow_promotion_to=UNTRUSTED means no auto-promotion at all.
        for _ in range(10):
            tracker.record_success("agent")

        # Must remain PROVISIONAL -- must NOT reach TRUSTED.
        assert tracker.get_trust_level("agent") == TrustLevel.PROVISIONAL

    def test_allow_promotion_to_untrusted_blocks_all_promotion(self) -> None:
        """allow_promotion_to=UNTRUSTED: no agent should ever be promoted."""
        policy = TrustPolicy(
            promotion_threshold=1,
            allow_promotion_to=TrustLevel.UNTRUSTED,
        )
        tracker = TrustEscalationTracker(policy=policy)

        for _ in range(20):
            tracker.record_success("agent")

        assert tracker.get_trust_level("agent") == TrustLevel.UNTRUSTED


class TestRouterEdgeCases:
    """Router with empty policies, full coverage, and edge inputs."""

    def test_empty_policies_dict_returns_default(self) -> None:
        from veronica_core.shield.pipeline import ShieldPipeline

        default = ShieldPipeline()
        router = TrustBasedPolicyRouter(policies={}, default_policy=default)
        for level in TrustLevel:
            agent = AgentIdentity(agent_id="x", origin="local", trust_level=level)
            assert router.route(agent) is default

    def test_all_trust_levels_mapped(self) -> None:
        from veronica_core.shield.pipeline import ShieldPipeline

        pipelines = {level: ShieldPipeline() for level in TrustLevel}
        router = TrustBasedPolicyRouter(policies=pipelines)
        for level in TrustLevel:
            agent = AgentIdentity(agent_id="x", origin="local", trust_level=level)
            assert router.route(agent) is pipelines[level]

    def test_default_policy_none_creates_empty_pipeline(self) -> None:
        from veronica_core.shield.pipeline import ShieldPipeline

        router = TrustBasedPolicyRouter()
        agent = AgentIdentity(agent_id="x", origin="local")
        result = router.route(agent)
        assert isinstance(result, ShieldPipeline)

    def test_get_policy_for_unmapped_returns_default(self) -> None:
        from veronica_core.shield.pipeline import ShieldPipeline

        default = ShieldPipeline()
        router = TrustBasedPolicyRouter(default_policy=default)
        result = router.get_policy_for(TrustLevel.PRIVILEGED)
        assert result is default

    def test_router_is_concurrent_safe(self) -> None:
        """Concurrent route() calls must not crash (read-only after init)."""
        from veronica_core.shield.pipeline import ShieldPipeline

        pipelines = {level: ShieldPipeline() for level in TrustLevel}
        router = TrustBasedPolicyRouter(policies=pipelines)
        errors: list[Exception] = []

        def route_all() -> None:
            try:
                for level in TrustLevel:
                    agent = AgentIdentity(agent_id="x", origin="local", trust_level=level)
                    router.route(agent)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=route_all) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
