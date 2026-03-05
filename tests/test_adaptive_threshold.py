"""Tests for AdaptiveThresholdPolicy."""

from __future__ import annotations

import time


from veronica_core.adaptive.burn_rate import BurnRateEstimator
from veronica_core.adaptive.threshold import AdaptiveConfig, AdaptiveThresholdPolicy
from veronica_core.runtime_policy import PolicyContext, RuntimePolicy


def _make_policy(
    remaining: float = 1000.0,
    cfg: AdaptiveConfig | None = None,
    alpha: float = 0.3,
) -> tuple[BurnRateEstimator, AdaptiveThresholdPolicy]:
    est = BurnRateEstimator(alpha=alpha)
    policy = AdaptiveThresholdPolicy(
        burn_rate=est, remaining_budget=remaining, config=cfg
    )
    return est, policy


class TestRuntimePolicyProtocol:
    def test_implements_runtime_policy(self):
        _, policy = _make_policy()
        assert isinstance(policy, RuntimePolicy)

    def test_policy_type(self):
        _, policy = _make_policy()
        assert policy.policy_type == "adaptive_threshold"

    def test_reset_is_callable(self):
        _, policy = _make_policy()
        policy.reset()  # Should not raise


class TestNoBurnRate:
    def test_no_burn_rate_allows(self):
        _, policy = _make_policy(remaining=100.0)
        ctx = PolicyContext()
        decision = policy.check(ctx)
        assert decision.allowed is True
        assert "No burn rate" in decision.reason


class TestExhaustionTiers:
    def _inject_rate(self, est: BurnRateEstimator, rate_per_sec: float) -> None:
        """Inject events to establish a known rate over 1 hour."""
        now = time.monotonic()
        # 60 events over 1 hour
        for i in range(60):
            est.record(rate_per_sec * 60.0, timestamp=now - 3600 + i * 60)

    def test_normal_burn_rate_allows(self):
        """Time-to-exhaustion > 24h → ALLOW."""
        est, policy = _make_policy(remaining=1_000_000.0)
        # Very low rate → budget lasts years
        now = time.monotonic()
        est.record(0.001, timestamp=now - 3600)
        est.record(0.001, timestamp=now)
        ctx = PolicyContext()
        decision = policy.check(ctx)
        assert decision.allowed is True

    def test_approaching_exhaustion_warns(self):
        """Time-to-exhaustion 6h < TTE < 24h → WARN (allowed=True)."""
        est, policy = _make_policy(
            remaining=100.0,
            cfg=AdaptiveConfig(
                warn_at_exhaustion_hours=24.0,
                degrade_at_exhaustion_hours=6.0,
                halt_at_exhaustion_hours=1.0,
            ),
        )
        # Rate = 100 / (12*3600) → TTE ≈ 12h
        rate_per_sec = 100.0 / (12 * 3600)
        now = time.monotonic()
        for i in range(60):
            est.record(rate_per_sec * 60, timestamp=now - 3600 + i * 60)
        ctx = PolicyContext()
        decision = policy.check(ctx)
        assert decision.allowed is True
        assert "WARN" in decision.reason

    def test_critical_exhaustion_degrades(self):
        """Time-to-exhaustion 1h < TTE < 6h → DEGRADE (allowed=True, degradation_action set)."""
        est, policy = _make_policy(
            remaining=100.0,
            cfg=AdaptiveConfig(
                warn_at_exhaustion_hours=24.0,
                degrade_at_exhaustion_hours=6.0,
                halt_at_exhaustion_hours=1.0,
            ),
        )
        # Rate = 100 / (3*3600) → TTE ≈ 3h
        rate_per_sec = 100.0 / (3 * 3600)
        now = time.monotonic()
        for i in range(60):
            est.record(rate_per_sec * 60, timestamp=now - 3600 + i * 60)
        ctx = PolicyContext()
        decision = policy.check(ctx)
        assert decision.allowed is True
        assert "DEGRADE" in decision.reason
        assert decision.degradation_action == "RATE_LIMIT"

    def test_emergency_exhaustion_halts(self):
        """Time-to-exhaustion < 1h → HALT (allowed=False)."""
        est, policy = _make_policy(
            remaining=100.0,
            cfg=AdaptiveConfig(
                warn_at_exhaustion_hours=24.0,
                degrade_at_exhaustion_hours=6.0,
                halt_at_exhaustion_hours=1.0,
            ),
        )
        # Rate = 100 / (0.5*3600) → TTE ≈ 0.5h
        rate_per_sec = 100.0 / (0.5 * 3600)
        now = time.monotonic()
        for i in range(60):
            est.record(rate_per_sec * 60, timestamp=now - 3600 + i * 60)
        ctx = PolicyContext()
        decision = policy.check(ctx)
        assert decision.allowed is False
        assert "HALT" in decision.reason


class TestSpikeDetection:
    def test_spike_triggers_degrade(self):
        """Instantaneous rate >> baseline → DEGRADE."""
        est, policy = _make_policy(
            remaining=10_000.0,
            cfg=AdaptiveConfig(spike_multiplier=3.0),
        )
        now = time.monotonic()
        # Baseline: low rate over past hour
        for i in range(60):
            est.record(0.01, timestamp=now - 3600 + i * 60)
        # Spike: huge events in last 60 seconds
        for i in range(10):
            est.record(100.0, timestamp=now - 60 + i * 6)
        ctx = PolicyContext()
        decision = policy.check(ctx)
        # Should detect spike and return DEGRADE
        assert decision.degradation_action == "RATE_LIMIT"
        assert "SPIKE" in decision.reason or "DEGRADE" in decision.reason


class TestZeroRemainingBudget:
    def test_zero_budget_halts(self):
        """remaining_budget=0 → immediate HALT regardless of burn rate."""
        est, policy = _make_policy(remaining=0.0)
        ctx = PolicyContext()
        decision = policy.check(ctx)
        assert decision.allowed is False
        assert "exhausted" in decision.reason.lower()


class TestRecovery:
    def test_budget_refill_restores_allow(self):
        """After budget refill, HALT → ALLOW."""
        est, policy = _make_policy(
            remaining=10.0,
            cfg=AdaptiveConfig(halt_at_exhaustion_hours=1.0),
        )
        # Set very high burn rate → TTE < 1h
        rate_per_sec = 10.0 / (0.1 * 3600)
        now = time.monotonic()
        for i in range(60):
            est.record(rate_per_sec * 60, timestamp=now - 3600 + i * 60)

        ctx = PolicyContext()
        decision = policy.check(ctx)
        assert decision.allowed is False

        # Refill budget
        policy.update_remaining_budget(1_000_000.0)
        decision2 = policy.check(ctx)
        # Now TTE is huge → should ALLOW (no spike)
        assert decision2.allowed is True


class TestFullEscalationCycle:
    def test_full_escalation_cycle(self):
        """ALLOW → WARN → DEGRADE → HALT → budget refill → ALLOW."""
        cfg = AdaptiveConfig(
            warn_at_exhaustion_hours=24.0,
            degrade_at_exhaustion_hours=6.0,
            halt_at_exhaustion_hours=1.0,
            spike_multiplier=100.0,  # disable spike detection
        )
        # 1. ALLOW: huge budget, tiny rate
        est = BurnRateEstimator()
        policy = AdaptiveThresholdPolicy(
            burn_rate=est, remaining_budget=1_000_000.0, config=cfg
        )
        now = time.monotonic()
        est.record(0.001, timestamp=now - 3600)
        est.record(0.001, timestamp=now)
        ctx = PolicyContext()
        d = policy.check(ctx)
        assert d.allowed is True
        assert "No burn rate" in d.reason or "ALLOW" in d.reason

        # 2. WARN: moderate rate, budget ~12h TTE
        est2 = BurnRateEstimator()
        policy2 = AdaptiveThresholdPolicy(
            burn_rate=est2, remaining_budget=100.0, config=cfg
        )
        rate_12h = 100.0 / (12 * 3600)
        for i in range(60):
            est2.record(rate_12h * 60, timestamp=now - 3600 + i * 60)
        d2 = policy2.check(ctx)
        assert d2.allowed is True
        assert "WARN" in d2.reason

        # 3. DEGRADE: 3h TTE
        est3 = BurnRateEstimator()
        policy3 = AdaptiveThresholdPolicy(
            burn_rate=est3, remaining_budget=100.0, config=cfg
        )
        rate_3h = 100.0 / (3 * 3600)
        for i in range(60):
            est3.record(rate_3h * 60, timestamp=now - 3600 + i * 60)
        d3 = policy3.check(ctx)
        assert d3.allowed is True
        assert d3.degradation_action == "RATE_LIMIT"

        # 4. HALT: 0.5h TTE
        est4 = BurnRateEstimator()
        policy4 = AdaptiveThresholdPolicy(
            burn_rate=est4, remaining_budget=100.0, config=cfg
        )
        rate_0h5 = 100.0 / (0.5 * 3600)
        for i in range(60):
            est4.record(rate_0h5 * 60, timestamp=now - 3600 + i * 60)
        d4 = policy4.check(ctx)
        assert d4.allowed is False

        # 5. Budget refill → ALLOW again
        policy4.update_remaining_budget(1_000_000_000.0)
        d5 = policy4.check(ctx)
        assert d5.allowed is True
