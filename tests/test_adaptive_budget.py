"""Tests for AdaptiveBudgetHook (v0.6.0 + v0.7.0 stabilization)."""

from __future__ import annotations

import threading

import pytest

from veronica_core.shield.adaptive_budget import (
    AdaptiveBudgetHook,
    AdjustmentResult,
)
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision, ToolCallContext

CTX = ToolCallContext(request_id="test-adaptive", tool_name="llm")


def _halt_event(event_type: str = "BUDGET_WINDOW_EXCEEDED") -> SafetyEvent:
    """Create a HALT SafetyEvent for testing."""
    return SafetyEvent(
        event_type=event_type,
        decision=Decision.HALT,
        reason="test",
        hook="TestHook",
    )


def _degrade_event(event_type: str = "BUDGET_WINDOW_EXCEEDED") -> SafetyEvent:
    """Create a DEGRADE SafetyEvent for testing."""
    return SafetyEvent(
        event_type=event_type,
        decision=Decision.DEGRADE,
        reason="test",
        hook="TestHook",
    )


# ---------------------------------------------------------------------------
# Construction & validation
# ---------------------------------------------------------------------------


class TestInit:
    def test_hook_starts_at_full_capacity_with_no_adjustment(self):
        hook = AdaptiveBudgetHook(base_ceiling=100)
        assert hook.base_ceiling == 100
        assert hook.ceiling_multiplier == 1.0
        assert hook.adjusted_ceiling == 100
        assert hook.window_seconds == 1800.0

    def test_hook_accepts_custom_tuning_parameters(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=200,
            window_seconds=600.0,
            tighten_trigger=5,
            tighten_pct=0.15,
            loosen_pct=0.08,
            max_adjustment=0.30,
        )
        assert hook.base_ceiling == 200
        assert hook.window_seconds == 600.0

    def test_hook_rejects_zero_base_ceiling(self):
        with pytest.raises(ValueError, match="base_ceiling must be positive"):
            AdaptiveBudgetHook(base_ceiling=0)

    def test_hook_rejects_negative_base_ceiling(self):
        with pytest.raises(ValueError, match="base_ceiling must be positive"):
            AdaptiveBudgetHook(base_ceiling=-10)

    def test_hook_rejects_zero_tighten_percentage(self):
        with pytest.raises(ValueError, match="tighten_pct"):
            AdaptiveBudgetHook(base_ceiling=100, tighten_pct=0.0)

    def test_hook_rejects_negative_loosen_percentage(self):
        with pytest.raises(ValueError, match="loosen_pct"):
            AdaptiveBudgetHook(base_ceiling=100, loosen_pct=-0.1)

    def test_hook_rejects_zero_max_adjustment(self):
        with pytest.raises(ValueError, match="max_adjustment"):
            AdaptiveBudgetHook(base_ceiling=100, max_adjustment=0.0)


# ---------------------------------------------------------------------------
# Event ingestion
# ---------------------------------------------------------------------------


class TestFeedEvents:
    def test_budget_hook_accepts_single_safety_event_without_error(self):
        hook = AdaptiveBudgetHook(base_ceiling=100)
        event = _halt_event()
        hook.feed_event(event, ts=1000.0)
        # No crash, event is buffered internally

    def test_budget_hook_accepts_batch_of_safety_events_without_error(self):
        hook = AdaptiveBudgetHook(base_ceiling=100)
        events = [_halt_event() for _ in range(5)]
        hook.feed_events(events)
        # No crash, 5 events buffered


# ---------------------------------------------------------------------------
# Adjust: hold
# ---------------------------------------------------------------------------


class TestAdjustHold:
    def test_budget_holds_steady_when_events_are_below_tighten_trigger(self):
        """Some degrade but below tighten threshold -> hold."""
        hook = AdaptiveBudgetHook(base_ceiling=100, tighten_trigger=3)
        # Feed 1 HALT (below trigger) + 1 DEGRADE (prevents loosen)
        hook.feed_event(_halt_event(), ts=1000.0)
        hook.feed_event(_degrade_event(), ts=1000.0)

        result = hook.adjust(_now=1000.0)
        assert result.action == "hold"
        assert result.adjusted_ceiling == 100
        assert result.ceiling_multiplier == 1.0

    def test_hold_action_produces_no_safety_events(self):
        hook = AdaptiveBudgetHook(base_ceiling=100, tighten_trigger=3)
        hook.feed_event(_halt_event(), ts=1000.0)
        hook.feed_event(_degrade_event(), ts=1000.0)
        hook.adjust(_now=1000.0)
        assert hook.get_events() == []


# ---------------------------------------------------------------------------
# Adjust: tighten
# ---------------------------------------------------------------------------


class TestAdjustTighten:
    def test_budget_ceiling_reduced_when_halt_events_reach_trigger_count(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, tighten_trigger=3, tighten_pct=0.10
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1000.0)

        result = hook.adjust(_now=1000.0)
        assert result.action == "tighten"
        assert result.ceiling_multiplier == 0.9
        assert result.adjusted_ceiling == 90

    def test_tighten_records_degrade_event(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, tighten_trigger=3, tighten_pct=0.10
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1000.0)

        hook.adjust(CTX, _now=1000.0)
        events = hook.get_events()
        assert len(events) == 1
        assert events[0].event_type == "ADAPTIVE_ADJUSTMENT"
        assert events[0].decision == Decision.DEGRADE

    def test_tighten_event_metadata(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, tighten_trigger=3, tighten_pct=0.10
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1000.0)

        hook.adjust(CTX, _now=1000.0)
        ev = hook.get_events()[0]
        assert ev.metadata["action"] == "tighten"
        assert ev.metadata["old_multiplier"] == 1.0
        assert ev.metadata["new_multiplier"] == 0.9
        assert ev.metadata["adjusted_ceiling"] == 90
        assert ev.metadata["tighten_events"] == 3
        assert ev.request_id == "test-adaptive"

    def test_budget_ceiling_stops_tightening_at_configured_minimum_multiplier(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            tighten_trigger=1,
            tighten_pct=0.15,
            max_adjustment=0.20,
        )
        # 3 tighten cycles: 1.0 -> 0.85 -> 0.80 (clamped at 0.80)
        for i in range(3):
            hook.feed_event(_halt_event(), ts=1000.0 + i)
            hook.adjust(_now=1000.0 + i)

        assert hook.ceiling_multiplier == 0.80
        assert hook.adjusted_ceiling == 80

    def test_budget_ceiling_tightens_further_on_each_adjustment_cycle(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, tighten_trigger=3, tighten_pct=0.05
        )
        # First round: 3 HALT events
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1000.0)
        result1 = hook.adjust(_now=1000.0)
        assert result1.ceiling_multiplier == 0.95

        # Second round: 3 more HALT events (total 6 still in window)
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1001.0)
        result2 = hook.adjust(_now=1001.0)
        assert result2.ceiling_multiplier == 0.90

    def test_token_budget_exceeded_events_also_trigger_tightening(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, tighten_trigger=3, tighten_pct=0.10
        )
        for _ in range(3):
            hook.feed_event(
                _halt_event("TOKEN_BUDGET_EXCEEDED"), ts=1000.0
            )

        result = hook.adjust(_now=1000.0)
        assert result.action == "tighten"

    def test_tighten_ignores_degrade_events_for_count(self):
        """Only HALT events count toward tighten trigger."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100, tighten_trigger=3, tighten_pct=0.10
        )
        # 2 HALT + 5 DEGRADE -> tighten_count = 2 < 3
        for _ in range(2):
            hook.feed_event(_halt_event(), ts=1000.0)
        for _ in range(5):
            hook.feed_event(_degrade_event(), ts=1000.0)

        result = hook.adjust(_now=1000.0)
        assert result.action == "hold"  # 2 < 3 trigger


# ---------------------------------------------------------------------------
# Adjust: loosen
# ---------------------------------------------------------------------------


class TestAdjustLoosen:
    def test_budget_ceiling_increases_when_no_degrade_events_occur_in_window(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, loosen_pct=0.05
        )
        # Empty window -> no degrade events -> loosen
        result = hook.adjust(_now=1000.0)
        assert result.action == "loosen"
        assert result.ceiling_multiplier == 1.05
        assert result.adjusted_ceiling == 105

    def test_loosen_action_emits_allow_safety_event(self):
        hook = AdaptiveBudgetHook(base_ceiling=100, loosen_pct=0.05)
        hook.adjust(CTX, _now=1000.0)
        events = hook.get_events()
        assert len(events) == 1
        assert events[0].event_type == "ADAPTIVE_ADJUSTMENT"
        assert events[0].decision == Decision.ALLOW

    def test_budget_ceiling_stops_loosening_at_configured_maximum_multiplier(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, loosen_pct=0.15, max_adjustment=0.20
        )
        # 3 loosen cycles: 1.0 -> 1.15 -> 1.20 (clamped at 1.20)
        for i in range(3):
            hook.adjust(_now=1000.0 + i)

        assert hook.ceiling_multiplier == 1.20
        assert hook.adjusted_ceiling == 120

    def test_budget_loosens_when_observation_window_contains_no_events(self):
        """No events at all -> zero degrade -> loosen."""
        hook = AdaptiveBudgetHook(base_ceiling=100, loosen_pct=0.05)
        result = hook.adjust(_now=5000.0)
        assert result.action == "loosen"
        assert result.degrade_events_in_window == 0


# ---------------------------------------------------------------------------
# Window expiry
# ---------------------------------------------------------------------------


class TestWindowExpiry:
    def test_halt_events_older_than_window_do_not_influence_tighten_decision(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=60.0,
            tighten_trigger=3,
        )
        # Feed 3 HALT at time 0
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=0.0)

        # At time 61, events are expired -> loosen (not tighten)
        result = hook.adjust(_now=61.0)
        assert result.action == "loosen"
        assert result.tighten_events_in_window == 0


# ---------------------------------------------------------------------------
# Tighten then loosen recovery
# ---------------------------------------------------------------------------


class TestTightenThenLoosen:
    def test_recovery_after_tighten(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=60.0,
            tighten_trigger=3,
            tighten_pct=0.10,
            loosen_pct=0.05,
        )
        # Tighten at time 0
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=0.0)
        r1 = hook.adjust(_now=0.0)
        assert r1.action == "tighten"
        assert r1.ceiling_multiplier == 0.90

        # At time 61, events expired -> loosen
        r2 = hook.adjust(_now=61.0)
        assert r2.action == "loosen"
        assert r2.ceiling_multiplier == 0.95

        # Another loosen
        r3 = hook.adjust(_now=62.0)
        assert r3.action == "loosen"
        assert r3.ceiling_multiplier == 1.0


# ---------------------------------------------------------------------------
# Custom event types
# ---------------------------------------------------------------------------


class TestCustomEventTypes:
    def test_custom_tighten_types(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            tighten_trigger=2,
            tighten_event_types=frozenset({"CUSTOM_EXCEEDED"}),
        )
        for _ in range(2):
            hook.feed_event(
                SafetyEvent(
                    event_type="CUSTOM_EXCEEDED",
                    decision=Decision.HALT,
                    reason="test",
                    hook="CustomHook",
                ),
                ts=1000.0,
            )
        result = hook.adjust(_now=1000.0)
        assert result.action == "tighten"

    def test_custom_degrade_types(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            degrade_event_types=frozenset({"MY_DEGRADE"}),
        )
        hook.feed_event(
            SafetyEvent(
                event_type="MY_DEGRADE",
                decision=Decision.DEGRADE,
                reason="test",
                hook="CustomHook",
            ),
            ts=1000.0,
        )
        result = hook.adjust(_now=1000.0)
        assert result.action == "hold"  # 1 degrade -> hold


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_adjusted_ceiling_never_below_one(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=1,
            tighten_trigger=1,
            tighten_pct=0.99,
            max_adjustment=0.99,
        )
        hook.feed_event(_halt_event(), ts=1000.0)
        result = hook.adjust(_now=1000.0)
        assert result.adjusted_ceiling >= 1

    def test_adjust_without_ctx(self):
        hook = AdaptiveBudgetHook(base_ceiling=100)
        result = hook.adjust(_now=1000.0)
        assert result.action == "loosen"
        events = hook.get_events()
        assert events[0].request_id is None


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


class TestStateManagement:
    def test_get_events_returns_copy(self):
        hook = AdaptiveBudgetHook(base_ceiling=100)
        hook.adjust(_now=1000.0)
        events1 = hook.get_events()
        events2 = hook.get_events()
        assert events1 is not events2
        assert len(events1) == len(events2)

    def test_clear_events(self):
        hook = AdaptiveBudgetHook(base_ceiling=100)
        hook.adjust(_now=1000.0)
        assert len(hook.get_events()) == 1
        hook.clear_events()
        assert len(hook.get_events()) == 0

    def test_reset(self):
        hook = AdaptiveBudgetHook(base_ceiling=100, loosen_pct=0.10)
        hook.adjust(_now=1000.0)  # loosen to 1.10
        assert hook.ceiling_multiplier == 1.10
        assert len(hook.get_events()) == 1

        hook.reset()
        assert hook.ceiling_multiplier == 1.0
        assert hook.adjusted_ceiling == 100
        assert len(hook.get_events()) == 0


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_feed_and_adjust(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            tighten_trigger=10,
            window_seconds=600.0,
        )
        errors: list[Exception] = []

        def feed_loop():
            try:
                for i in range(50):
                    hook.feed_event(_halt_event(), ts=1000.0 + i * 0.1)
            except Exception as e:
                errors.append(e)

        def adjust_loop():
            try:
                for i in range(20):
                    hook.adjust(_now=1000.0 + i * 0.5)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=feed_loop),
            threading.Thread(target=adjust_loop),
            threading.Thread(target=feed_loop),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


# ---------------------------------------------------------------------------
# Config round-trip
# ---------------------------------------------------------------------------


class TestConfigRoundTrip:
    def test_config_defaults(self):
        from veronica_core.shield.config import AdaptiveBudgetConfig

        cfg = AdaptiveBudgetConfig()
        assert cfg.enabled is False
        assert cfg.window_seconds == 1800.0
        assert cfg.tighten_trigger == 3
        assert cfg.tighten_pct == 0.10
        assert cfg.loosen_pct == 0.05
        assert cfg.max_adjustment_pct == 0.20

    def test_shield_config_round_trip(self):
        from veronica_core.shield.config import (
            AdaptiveBudgetConfig,
            ShieldConfig,
        )

        cfg = ShieldConfig(
            adaptive_budget=AdaptiveBudgetConfig(
                enabled=True,
                window_seconds=900.0,
                tighten_trigger=5,
            )
        )
        d = cfg.to_dict()
        restored = ShieldConfig.from_dict(d)
        assert restored.adaptive_budget.enabled is True
        assert restored.adaptive_budget.window_seconds == 900.0
        assert restored.adaptive_budget.tighten_trigger == 5

    def test_shield_config_is_any_enabled(self):
        from veronica_core.shield.config import (
            AdaptiveBudgetConfig,
            ShieldConfig,
        )

        cfg = ShieldConfig(
            adaptive_budget=AdaptiveBudgetConfig(enabled=True)
        )
        assert cfg.is_any_enabled is True

    def test_shield_config_default_disabled(self):
        from veronica_core.shield.config import ShieldConfig

        cfg = ShieldConfig()
        assert cfg.adaptive_budget.enabled is False
        assert cfg.is_any_enabled is False


# ---------------------------------------------------------------------------
# AdjustmentResult
# ---------------------------------------------------------------------------


class TestAdjustmentResult:
    def test_result_fields(self):
        result = AdjustmentResult(
            action="tighten",
            adjusted_ceiling=90,
            ceiling_multiplier=0.9,
            base_ceiling=100,
            tighten_events_in_window=3,
            degrade_events_in_window=1,
        )
        assert result.action == "tighten"
        assert result.adjusted_ceiling == 90
        assert result.ceiling_multiplier == 0.9
        assert result.base_ceiling == 100
        assert result.tighten_events_in_window == 3
        assert result.degrade_events_in_window == 1

    def test_result_is_frozen(self):
        result = AdjustmentResult(
            action="hold",
            adjusted_ceiling=100,
            ceiling_multiplier=1.0,
            base_ceiling=100,
            tighten_events_in_window=0,
            degrade_events_in_window=0,
        )
        with pytest.raises(AttributeError):
            result.action = "loosen"  # type: ignore[misc]


# ===========================================================================
# v0.7.0 Stabilization Tests
# ===========================================================================


# ---------------------------------------------------------------------------
# Cooldown window
# ---------------------------------------------------------------------------


class TestCooldown:
    def test_cooldown_disabled_by_default(self):
        """Default cooldown_seconds=0 means no cooldown."""
        hook = AdaptiveBudgetHook(base_ceiling=100)
        assert hook.cooldown_seconds == 0.0
        # Two rapid adjustments should both succeed
        r1 = hook.adjust(_now=1000.0)
        r2 = hook.adjust(_now=1000.1)
        assert r1.action == "loosen"
        assert r2.action == "loosen"

    def test_cooldown_blocks_rapid_adjustment(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, cooldown_seconds=60.0
        )
        # First adjustment succeeds (no prior adjustment)
        r1 = hook.adjust(_now=1000.0)
        assert r1.action == "loosen"
        # Second adjustment within 60s -> blocked
        r2 = hook.adjust(_now=1030.0)
        assert r2.action == "cooldown_blocked"
        assert r2.adjusted_ceiling == r1.adjusted_ceiling

    def test_cooldown_expires(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, cooldown_seconds=60.0
        )
        r1 = hook.adjust(_now=1000.0)
        assert r1.action == "loosen"
        # After cooldown expires
        r2 = hook.adjust(_now=1061.0)
        assert r2.action == "loosen"

    def test_cooldown_records_event(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, cooldown_seconds=60.0
        )
        hook.adjust(CTX, _now=1000.0)
        hook.adjust(CTX, _now=1030.0)  # blocked
        events = hook.get_events()
        # First: ADAPTIVE_ADJUSTMENT (loosen), Second: ADAPTIVE_COOLDOWN_BLOCKED
        assert len(events) == 2
        assert events[1].event_type == "ADAPTIVE_COOLDOWN_BLOCKED"
        assert events[1].decision == Decision.DEGRADE
        assert events[1].request_id == "test-adaptive"

    def test_cooldown_event_metadata(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, cooldown_seconds=60.0
        )
        hook.adjust(_now=1000.0)
        hook.adjust(_now=1030.0)  # blocked
        ev = hook.get_events()[1]
        assert ev.metadata["elapsed_seconds"] == 30.0
        assert ev.metadata["cooldown_seconds"] == 60.0
        assert ev.metadata["remaining_seconds"] == 30.0

    def test_cooldown_includes_event_counts(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            cooldown_seconds=60.0,
            tighten_trigger=3,
        )
        hook.adjust(_now=1000.0)  # loosen
        # Feed 3 HALT events
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1010.0)
        # Blocked by cooldown, but counts should be reported
        r = hook.adjust(_now=1020.0)
        assert r.action == "cooldown_blocked"
        assert r.tighten_events_in_window == 3

    def test_cooldown_not_updated_on_hold(self):
        """Hold actions don't update the cooldown timestamp."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            cooldown_seconds=60.0,
            tighten_trigger=3,
        )
        # loosen at t=1000 -> sets last_adjustment_ts=1000
        hook.adjust(_now=1000.0)
        assert hook.last_adjustment_ts == 1000.0

        # feed 1 HALT + 1 DEGRADE -> hold (doesn't update timestamp)
        hook.feed_event(_halt_event(), ts=1070.0)
        hook.feed_event(_degrade_event(), ts=1070.0)
        r = hook.adjust(_now=1070.0)
        assert r.action == "hold"
        assert hook.last_adjustment_ts == 1000.0

    def test_cooldown_reset_clears_timestamp(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, cooldown_seconds=60.0
        )
        hook.adjust(_now=1000.0)
        assert hook.last_adjustment_ts == 1000.0
        hook.reset()
        assert hook.last_adjustment_ts is None


# ---------------------------------------------------------------------------
# Adjustment smoothing (max_step_pct)
# ---------------------------------------------------------------------------


class TestSmoothing:
    def test_default_no_smoothing(self):
        """Default max_step_pct=1.0 means no per-step cap."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100, loosen_pct=0.15
        )
        assert hook.max_step_pct == 1.0
        r = hook.adjust(_now=1000.0)
        assert r.ceiling_multiplier == 1.15  # full loosen_pct applied

    def test_smoothing_caps_loosen(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, loosen_pct=0.10, max_step_pct=0.05
        )
        r = hook.adjust(_now=1000.0)
        # loosen_pct=0.10 but capped by max_step_pct=0.05
        assert r.ceiling_multiplier == 1.05

    def test_smoothing_caps_tighten(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            tighten_trigger=3,
            tighten_pct=0.10,
            max_step_pct=0.05,
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1000.0)
        r = hook.adjust(_now=1000.0)
        # tighten_pct=0.10 but capped by max_step_pct=0.05
        assert r.ceiling_multiplier == 0.95

    def test_smoothing_no_effect_when_step_larger(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, loosen_pct=0.03, max_step_pct=0.05
        )
        r = hook.adjust(_now=1000.0)
        # loosen_pct=0.03 < max_step_pct=0.05 -> no cap
        assert r.ceiling_multiplier == 1.03

    def test_smoothing_gradual_convergence(self):
        """Multiple capped steps converge gradually."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100, loosen_pct=0.10, max_step_pct=0.03
        )
        for i in range(5):
            hook.adjust(_now=1000.0 + i)
        # 5 steps at +0.03 each = 1.15
        assert hook.ceiling_multiplier == pytest.approx(1.15, abs=0.001)


# ---------------------------------------------------------------------------
# Hard floor/ceiling (min_multiplier, max_multiplier)
# ---------------------------------------------------------------------------


class TestFloorCeiling:
    def test_defaults_from_max_adjustment(self):
        """Without explicit min/max, computed from max_adjustment."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100, max_adjustment=0.30
        )
        assert hook.min_multiplier == pytest.approx(0.70)
        assert hook.max_multiplier == pytest.approx(1.30)

    def test_explicit_min_multiplier(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, min_multiplier=0.50
        )
        assert hook.min_multiplier == 0.50

    def test_explicit_max_multiplier(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, max_multiplier=1.50
        )
        assert hook.max_multiplier == 1.50

    def test_floor_enforced_on_tighten(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            tighten_trigger=1,
            tighten_pct=0.50,
            min_multiplier=0.60,
            max_multiplier=1.40,
        )
        hook.feed_event(_halt_event(), ts=1000.0)
        r = hook.adjust(_now=1000.0)
        # Would be 1.0 - 0.50 = 0.50 but clamped to 0.60
        assert r.ceiling_multiplier == 0.60
        assert r.adjusted_ceiling == 60

    def test_ceiling_enforced_on_loosen(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            loosen_pct=0.50,
            min_multiplier=0.60,
            max_multiplier=1.10,
        )
        r = hook.adjust(_now=1000.0)
        # Would be 1.0 + 0.50 = 1.50 but clamped to 1.10
        assert r.ceiling_multiplier == 1.10
        assert r.adjusted_ceiling == 110

    def test_validates_min_positive(self):
        with pytest.raises(ValueError, match="min_multiplier must be > 0"):
            AdaptiveBudgetHook(
                base_ceiling=100, min_multiplier=0.0
            )

    def test_validates_min_less_than_max(self):
        with pytest.raises(ValueError, match="min_multiplier.*must be <"):
            AdaptiveBudgetHook(
                base_ceiling=100,
                min_multiplier=1.5,
                max_multiplier=1.0,
            )

    def test_validates_min_equal_max(self):
        with pytest.raises(ValueError, match="min_multiplier.*must be <"):
            AdaptiveBudgetHook(
                base_ceiling=100,
                min_multiplier=1.0,
                max_multiplier=1.0,
            )

    def test_wider_floor_than_max_adjustment(self):
        """Explicit min_multiplier can be wider than max_adjustment implies."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            max_adjustment=0.10,  # would imply min=0.90
            min_multiplier=0.50,  # but explicit floor is wider
            tighten_trigger=1,
            tighten_pct=0.15,
        )
        hook.feed_event(_halt_event(), ts=1000.0)
        r1 = hook.adjust(_now=1000.0)
        assert r1.ceiling_multiplier == 0.85  # 1.0 - 0.15

        hook.feed_event(_halt_event(), ts=1001.0)
        r2 = hook.adjust(_now=1001.0)
        assert r2.ceiling_multiplier == 0.70  # 0.85 - 0.15

        hook.feed_event(_halt_event(), ts=1002.0)
        r3 = hook.adjust(_now=1002.0)
        assert r3.ceiling_multiplier == 0.55  # 0.70 - 0.15

        hook.feed_event(_halt_event(), ts=1003.0)
        r4 = hook.adjust(_now=1003.0)
        assert r4.ceiling_multiplier == 0.50  # clamped at floor


# ---------------------------------------------------------------------------
# Validation of new params
# ---------------------------------------------------------------------------


class TestNewParamValidation:
    def test_validates_cooldown_negative(self):
        with pytest.raises(ValueError, match="cooldown_seconds"):
            AdaptiveBudgetHook(base_ceiling=100, cooldown_seconds=-1.0)

    def test_validates_max_step_pct_zero(self):
        with pytest.raises(ValueError, match="max_step_pct"):
            AdaptiveBudgetHook(base_ceiling=100, max_step_pct=0.0)

    def test_validates_max_step_pct_over_one(self):
        with pytest.raises(ValueError, match="max_step_pct"):
            AdaptiveBudgetHook(base_ceiling=100, max_step_pct=1.5)

    def test_cooldown_zero_is_valid(self):
        hook = AdaptiveBudgetHook(base_ceiling=100, cooldown_seconds=0.0)
        assert hook.cooldown_seconds == 0.0


# ---------------------------------------------------------------------------
# Config round-trip (v0.7.0 fields)
# ---------------------------------------------------------------------------


class TestConfigRoundTripV07:
    def test_config_new_defaults(self):
        from veronica_core.shield.config import AdaptiveBudgetConfig

        cfg = AdaptiveBudgetConfig()
        assert cfg.cooldown_minutes == 15.0
        assert cfg.max_step_pct == 0.05
        assert cfg.min_multiplier == 0.6
        assert cfg.max_multiplier == 1.2

    def test_shield_config_round_trip_v07(self):
        from veronica_core.shield.config import (
            AdaptiveBudgetConfig,
            ShieldConfig,
        )

        cfg = ShieldConfig(
            adaptive_budget=AdaptiveBudgetConfig(
                enabled=True,
                cooldown_minutes=10.0,
                max_step_pct=0.03,
                min_multiplier=0.50,
                max_multiplier=1.50,
            )
        )
        d = cfg.to_dict()
        restored = ShieldConfig.from_dict(d)
        assert restored.adaptive_budget.cooldown_minutes == 10.0
        assert restored.adaptive_budget.max_step_pct == 0.03
        assert restored.adaptive_budget.min_multiplier == 0.50
        assert restored.adaptive_budget.max_multiplier == 1.50


# ---------------------------------------------------------------------------
# Combined: cooldown + smoothing + floor/ceiling
# ---------------------------------------------------------------------------


class TestCombinedStabilization:
    def test_all_features_together(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            tighten_trigger=3,
            tighten_pct=0.10,
            loosen_pct=0.10,
            cooldown_seconds=60.0,
            max_step_pct=0.05,
            min_multiplier=0.70,
            max_multiplier=1.10,
        )
        # Feed 3 HALT -> tighten (capped at 0.05)
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1000.0)
        r1 = hook.adjust(_now=1000.0)
        assert r1.action == "tighten"
        assert r1.ceiling_multiplier == 0.95  # 1.0 - 0.05 (capped)

        # Immediate retry -> cooldown blocked
        r2 = hook.adjust(_now=1030.0)
        assert r2.action == "cooldown_blocked"

        # After cooldown + events expired -> loosen (capped)
        r3 = hook.adjust(_now=2900.0)
        assert r3.action == "loosen"
        assert r3.ceiling_multiplier == 1.0  # 0.95 + 0.05 (capped)

    def test_floor_with_smoothing(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            tighten_trigger=1,
            tighten_pct=0.20,
            max_step_pct=0.05,
            min_multiplier=0.90,
        )
        # Each tighten is capped at 0.05
        for i in range(3):
            hook.feed_event(_halt_event(), ts=1000.0 + i)
            hook.adjust(_now=1000.0 + i)
        # 1.0 -> 0.95 -> 0.90 (floor hit)
        assert hook.ceiling_multiplier == pytest.approx(0.90)


# ---------------------------------------------------------------------------
# Direction lock (v0.7.0)
# ---------------------------------------------------------------------------


class TestDirectionLock:
    def test_disabled_by_default(self):
        hook = AdaptiveBudgetHook(base_ceiling=100)
        assert hook.direction_lock is False

    def test_enabled_via_param(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, direction_lock=True
        )
        assert hook.direction_lock is True

    def test_blocks_loosen_after_tighten(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=100.0,
            tighten_trigger=3,
            tighten_pct=0.10,
            loosen_pct=0.05,
            direction_lock=True,
        )
        # Tighten: 2 events at t=900, 1 at t=950
        hook.feed_event(_halt_event(), ts=900.0)
        hook.feed_event(_halt_event(), ts=900.0)
        hook.feed_event(_halt_event(), ts=950.0)
        r1 = hook.adjust(_now=950.0)
        assert r1.action == "tighten"
        assert hook.last_action == "tighten"

        # At t=1001: events at t=900 expired, 1 remains
        # tighten_count=1 < 3, degrade_count=0 -> would loosen, but locked
        r2 = hook.adjust(_now=1001.0)
        assert r2.action == "direction_locked"
        assert r2.ceiling_multiplier == 0.90  # unchanged

    def test_allows_loosen_when_exceeded_clear(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=60.0,
            tighten_trigger=3,
            tighten_pct=0.10,
            loosen_pct=0.05,
            direction_lock=True,
        )
        # Tighten
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1000.0)
        r1 = hook.adjust(_now=1000.0)
        assert r1.action == "tighten"

        # After window expires, exceeded events = 0 -> loosen allowed
        r2 = hook.adjust(_now=1061.0)
        assert r2.action == "loosen"
        assert hook.last_action == "loosen"

    def test_no_block_without_direction_lock(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=100.0,
            tighten_trigger=3,
            tighten_pct=0.10,
            loosen_pct=0.05,
            direction_lock=False,
        )
        # 2 at t=900, 1 at t=950
        hook.feed_event(_halt_event(), ts=900.0)
        hook.feed_event(_halt_event(), ts=900.0)
        hook.feed_event(_halt_event(), ts=950.0)
        r1 = hook.adjust(_now=950.0)
        assert r1.action == "tighten"

        # At t=1001: 1 event remains, tighten_count=1 < 3
        # Without direction_lock, loosen is allowed
        r2 = hook.adjust(_now=1001.0)
        assert r2.action == "loosen"

    def test_records_direction_locked_event(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=100.0,
            tighten_trigger=3,
            direction_lock=True,
        )
        hook.feed_event(_halt_event(), ts=900.0)
        hook.feed_event(_halt_event(), ts=900.0)
        hook.feed_event(_halt_event(), ts=950.0)
        hook.adjust(CTX, _now=950.0)  # tighten
        hook.adjust(CTX, _now=1001.0)  # direction_locked (1 event remains)
        events = hook.get_events()
        locked_events = [
            e for e in events
            if e.event_type == "ADAPTIVE_DIRECTION_LOCKED"
        ]
        assert len(locked_events) == 1
        assert locked_events[0].decision == Decision.DEGRADE
        assert locked_events[0].request_id == "test-adaptive"

    def test_direction_locked_event_metadata(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=100.0,
            tighten_trigger=3,
            direction_lock=True,
        )
        hook.feed_event(_halt_event(), ts=900.0)
        hook.feed_event(_halt_event(), ts=900.0)
        hook.feed_event(_halt_event(), ts=950.0)
        hook.adjust(_now=950.0)  # tighten
        hook.adjust(_now=1001.0)  # direction_locked (1 remains at t=950)
        ev = [
            e for e in hook.get_events()
            if e.event_type == "ADAPTIVE_DIRECTION_LOCKED"
        ][0]
        assert ev.metadata["tighten_events"] == 1
        assert "ceiling_multiplier" in ev.metadata

    def test_direction_lock_does_not_change_multiplier(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=100.0,
            tighten_trigger=3,
            tighten_pct=0.10,
            direction_lock=True,
        )
        hook.feed_event(_halt_event(), ts=900.0)
        hook.feed_event(_halt_event(), ts=900.0)
        hook.feed_event(_halt_event(), ts=950.0)
        hook.adjust(_now=950.0)  # tighten -> 0.90
        assert hook.ceiling_multiplier == 0.90

        hook.adjust(_now=1001.0)  # direction_locked (1 remains)
        assert hook.ceiling_multiplier == 0.90  # unchanged

    def test_direction_lock_allows_further_tighten(self):
        """Direction lock doesn't prevent tighten, only loosen."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            tighten_pct=0.05,
            direction_lock=True,
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1000.0)
        r1 = hook.adjust(_now=1000.0)
        assert r1.action == "tighten"

        # Feed more HALT events -> tighten again
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1001.0)
        r2 = hook.adjust(_now=1001.0)
        assert r2.action == "tighten"

    def test_last_action_after_loosen(self):
        """After loosen, direction lock doesn't block next loosen."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            loosen_pct=0.05,
            direction_lock=True,
        )
        r1 = hook.adjust(_now=1000.0)
        assert r1.action == "loosen"
        assert hook.last_action == "loosen"

        r2 = hook.adjust(_now=1001.0)
        assert r2.action == "loosen"  # no block

    def test_reset_clears_last_action(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, direction_lock=True
        )
        hook.adjust(_now=1000.0)  # loosen
        assert hook.last_action == "loosen"
        hook.reset()
        assert hook.last_action is None


# ---------------------------------------------------------------------------
# Config round-trip (direction_lock)
# ---------------------------------------------------------------------------


class TestConfigDirectionLock:
    def test_config_default_enabled(self):
        from veronica_core.shield.config import AdaptiveBudgetConfig

        cfg = AdaptiveBudgetConfig()
        assert cfg.direction_lock is True

    def test_shield_config_round_trip_direction_lock(self):
        from veronica_core.shield.config import (
            AdaptiveBudgetConfig,
            ShieldConfig,
        )

        cfg = ShieldConfig(
            adaptive_budget=AdaptiveBudgetConfig(
                enabled=True, direction_lock=False
            )
        )
        d = cfg.to_dict()
        restored = ShieldConfig.from_dict(d)
        assert restored.adaptive_budget.direction_lock is False


# ===========================================================================
# v0.7.0 Anomaly Tightening Tests
# ===========================================================================


# ---------------------------------------------------------------------------
# Anomaly: basic spike detection
# ---------------------------------------------------------------------------


class TestAnomalySpike:
    def test_disabled_by_default(self):
        hook = AdaptiveBudgetHook(base_ceiling=100)
        assert hook.anomaly_enabled is False
        assert hook.anomaly_active is False

    def test_no_anomaly_when_disabled(self):
        """With anomaly_enabled=False, spikes are ignored."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=1800.0,
            tighten_trigger=3,
            anomaly_enabled=False,
            anomaly_spike_factor=3.0,
            anomaly_recent_seconds=300.0,
        )
        # All 5 HALT events in recent window (clear spike)
        for _ in range(5):
            hook.feed_event(_halt_event(), ts=1700.0)
        hook.adjust(_now=1700.0)
        assert hook.anomaly_active is False

    def test_spike_activates_anomaly(self):
        """Concentrated events in recent window trigger anomaly."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=1800.0,
            tighten_trigger=3,
            anomaly_enabled=True,
            anomaly_spike_factor=3.0,
            anomaly_recent_seconds=300.0,
            anomaly_tighten_pct=0.15,
        )
        # Spread 3 events across the full window (avg = 3/6 = 0.5/period)
        hook.feed_event(_halt_event(), ts=100.0)
        hook.feed_event(_halt_event(), ts=600.0)
        hook.feed_event(_halt_event(), ts=1100.0)
        # Spike: 4 events in last 300s (4 > 3.0 * 1.0 = 3.0)
        # Total = 7, avg = 7/6 = 1.167, threshold = 3*1.167 = 3.5
        # 4 > 3.5 -> spike!
        for _ in range(4):
            hook.feed_event(_halt_event(), ts=1600.0)
        r = hook.adjust(_now=1700.0)
        assert hook.anomaly_active is True
        # Ceiling reduced by anomaly factor: 100 * 1.0 * 0.85 = 85
        # (tighten also fires: 7 >= 3)
        assert r.anomaly_active is True

    def test_spike_reduces_adjusted_ceiling(self):
        """Anomaly factor reduces the effective ceiling."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            tighten_pct=0.10,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
            anomaly_tighten_pct=0.15,
        )
        # All 3 events in recent window (spike: avg=3/6=0.5, 3>2*0.5=1)
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=550.0)
        r = hook.adjust(_now=550.0)
        # Tighten: multiplier = 0.90
        # Anomaly: factor = 0.85
        # Ceiling: 100 * 0.90 * 0.85 = 76.5 -> 76 (banker's rounding)
        assert r.action == "tighten"
        assert r.anomaly_active is True
        assert r.adjusted_ceiling == 76

    def test_no_spike_below_tighten_trigger(self):
        """Spike detection requires at least tighten_trigger recent events."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
        )
        # Only 2 recent events (below trigger of 3)
        hook.feed_event(_halt_event(), ts=550.0)
        hook.feed_event(_halt_event(), ts=550.0)
        hook.adjust(_now=550.0)
        assert hook.anomaly_active is False

    def test_no_spike_when_evenly_distributed(self):
        """Evenly distributed events do not trigger spike."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            anomaly_enabled=True,
            anomaly_spike_factor=3.0,
            anomaly_recent_seconds=100.0,
        )
        # 6 events, 1 per 100s period: avg = 6/6 = 1.0
        # Recent (last 100s): 1 event, 1 < 3 * 1.0 = 3.0
        for i in range(6):
            hook.feed_event(_halt_event(), ts=100.0 + i * 100.0)
        hook.adjust(_now=600.0)
        assert hook.anomaly_active is False

    def test_spike_not_reactivated_when_already_active(self):
        """Once active, anomaly doesn't re-trigger on more spikes."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
            anomaly_window_seconds=300.0,
        )
        # First spike
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=550.0)
        hook.adjust(_now=550.0)
        assert hook.anomaly_active is True
        first_ts = hook.anomaly_activated_ts

        # More events, still within anomaly window
        for _ in range(5):
            hook.feed_event(_halt_event(), ts=600.0)
        hook.adjust(_now=600.0)
        # Still active, timestamp unchanged
        assert hook.anomaly_active is True
        assert hook.anomaly_activated_ts == first_ts


# ---------------------------------------------------------------------------
# Anomaly: auto-recovery
# ---------------------------------------------------------------------------


class TestAnomalyRecovery:
    def test_auto_recovery_after_window(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
            anomaly_tighten_pct=0.15,
            anomaly_window_seconds=300.0,
        )
        # Activate anomaly
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=550.0)
        hook.adjust(_now=550.0)
        assert hook.anomaly_active is True
        assert hook.adjusted_ceiling < 100  # reduced

        # After anomaly window expires
        r = hook.adjust(_now=851.0)  # 550 + 300 + 1
        assert hook.anomaly_active is False
        assert r.anomaly_active is False

    def test_recovery_records_event(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
            anomaly_window_seconds=300.0,
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=550.0)
        hook.adjust(CTX, _now=550.0)
        hook.adjust(CTX, _now=851.0)
        events = hook.get_events()
        recovery_events = [
            e for e in events if e.event_type == "ANOMALY_RECOVERED"
        ]
        assert len(recovery_events) == 1
        assert recovery_events[0].decision == Decision.ALLOW
        assert recovery_events[0].request_id == "test-adaptive"

    def test_recovery_restores_ceiling(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=100.0,
            tighten_trigger=3,
            tighten_pct=0.10,
            loosen_pct=0.05,
            anomaly_enabled=True,
            anomaly_spike_factor=1.5,
            anomaly_recent_seconds=50.0,
            anomaly_tighten_pct=0.15,
            anomaly_window_seconds=200.0,
        )
        # Activate anomaly with tighten
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=900.0)
        r1 = hook.adjust(_now=900.0)
        assert r1.anomaly_active is True
        assert r1.ceiling_multiplier == 0.90
        # 100 * 0.90 * 0.85 = 76.5 -> 76
        assert r1.adjusted_ceiling == 76

        # At t=1101: events expired (900+100=1000), anomaly recovered (900+200=1100)
        # Empty window -> loosen: 0.90 + 0.05 = 0.95
        r2 = hook.adjust(_now=1101.0)
        assert r2.anomaly_active is False
        assert r2.action == "loosen"
        # 100 * 0.95 * 1.0 = 95 (anomaly factor removed)
        assert hook.adjusted_ceiling == 95

    def test_can_reactivate_after_recovery(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
            anomaly_window_seconds=200.0,
        )
        # First activation
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=500.0)
        hook.adjust(_now=500.0)
        assert hook.anomaly_active is True

        # Recovery
        hook.adjust(_now=701.0)
        assert hook.anomaly_active is False

        # Second spike -> re-activate
        for _ in range(4):
            hook.feed_event(_halt_event(), ts=750.0)
        hook.adjust(_now=750.0)
        assert hook.anomaly_active is True


# ---------------------------------------------------------------------------
# Anomaly: events and metadata
# ---------------------------------------------------------------------------


class TestAnomalyEvents:
    def test_activation_records_event(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=550.0)
        hook.adjust(CTX, _now=550.0)
        events = hook.get_events()
        anomaly_events = [
            e for e in events
            if e.event_type == "ANOMALY_TIGHTENING_APPLIED"
        ]
        assert len(anomaly_events) == 1
        assert anomaly_events[0].decision == Decision.DEGRADE
        assert anomaly_events[0].request_id == "test-adaptive"

    def test_activation_event_metadata(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
            anomaly_tighten_pct=0.15,
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=550.0)
        hook.adjust(_now=550.0)
        ev = [
            e for e in hook.get_events()
            if e.event_type == "ANOMALY_TIGHTENING_APPLIED"
        ][0]
        assert ev.metadata["recent_tighten_count"] == 3
        assert ev.metadata["spike_factor"] == 2.0
        assert ev.metadata["anomaly_tighten_pct"] == 0.15
        assert "avg_per_period" in ev.metadata

    def test_recovery_event_metadata(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
            anomaly_window_seconds=300.0,
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=550.0)
        hook.adjust(_now=550.0)
        hook.adjust(_now=851.0)
        ev = [
            e for e in hook.get_events()
            if e.event_type == "ANOMALY_RECOVERED"
        ][0]
        assert ev.metadata["anomaly_window_seconds"] == 300.0


# ---------------------------------------------------------------------------
# Anomaly: interaction with other features
# ---------------------------------------------------------------------------


class TestAnomalyInteraction:
    def test_anomaly_with_cooldown(self):
        """Anomaly detection works during cooldown check."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            cooldown_seconds=60.0,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
            anomaly_tighten_pct=0.15,
        )
        # First adjust to set cooldown
        hook.adjust(_now=500.0)  # loosen

        # Spike events while in cooldown
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=520.0)

        # Cooldown blocked, but anomaly should still activate
        r = hook.adjust(_now=520.0)
        assert r.action == "cooldown_blocked"
        assert hook.anomaly_active is True
        # Cooldown ceiling should include anomaly factor
        assert r.anomaly_active is True

    def test_anomaly_orthogonal_to_multiplier(self):
        """Anomaly factor is independent of ceiling_multiplier."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=100.0,
            tighten_trigger=3,
            tighten_pct=0.10,
            loosen_pct=0.05,
            anomaly_enabled=True,
            anomaly_spike_factor=1.5,
            anomaly_recent_seconds=50.0,
            anomaly_tighten_pct=0.15,
            anomaly_window_seconds=150.0,
        )
        # Activate anomaly
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=900.0)
        r = hook.adjust(_now=900.0)
        assert r.ceiling_multiplier == 0.90  # normal tighten
        assert r.anomaly_active is True
        # 100 * 0.90 * 0.85 = 76.5 -> 76 (banker's rounding)
        assert r.adjusted_ceiling == 76

        # At t=1051: events expired (900+100=1000), anomaly recovered (900+150=1050)
        # loosen: 0.90 + 0.05 = 0.95
        r2 = hook.adjust(_now=1051.0)
        assert r2.anomaly_active is False
        assert r2.ceiling_multiplier == 0.95
        assert hook.adjusted_ceiling == 95  # 100 * 0.95 * 1.0

    def test_anomaly_result_flag_without_anomaly(self):
        """anomaly_active=False when feature not enabled."""
        hook = AdaptiveBudgetHook(base_ceiling=100)
        r = hook.adjust(_now=1000.0)
        assert r.anomaly_active is False


# ---------------------------------------------------------------------------
# Anomaly: reset
# ---------------------------------------------------------------------------


class TestAnomalyReset:
    def test_reset_clears_anomaly_state(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=550.0)
        hook.adjust(_now=550.0)
        assert hook.anomaly_active is True

        hook.reset()
        assert hook.anomaly_active is False
        assert hook.anomaly_activated_ts is None


# ---------------------------------------------------------------------------
# Anomaly: validation
# ---------------------------------------------------------------------------


class TestAnomalyValidation:
    def test_validates_spike_factor_positive(self):
        with pytest.raises(ValueError, match="anomaly_spike_factor"):
            AdaptiveBudgetHook(
                base_ceiling=100, anomaly_spike_factor=0.0
            )

    def test_validates_tighten_pct_range(self):
        with pytest.raises(ValueError, match="anomaly_tighten_pct"):
            AdaptiveBudgetHook(
                base_ceiling=100, anomaly_tighten_pct=0.0
            )

    def test_validates_window_seconds_positive(self):
        with pytest.raises(ValueError, match="anomaly_window_seconds"):
            AdaptiveBudgetHook(
                base_ceiling=100, anomaly_window_seconds=0.0
            )

    def test_validates_recent_seconds_positive(self):
        with pytest.raises(ValueError, match="anomaly_recent_seconds"):
            AdaptiveBudgetHook(
                base_ceiling=100, anomaly_recent_seconds=-1.0
            )


# ---------------------------------------------------------------------------
# Anomaly: config round-trip
# ---------------------------------------------------------------------------


class TestConfigAnomalyRoundTrip:
    def test_config_anomaly_defaults(self):
        from veronica_core.shield.config import AdaptiveBudgetConfig

        cfg = AdaptiveBudgetConfig()
        assert cfg.anomaly_enabled is False
        assert cfg.anomaly_spike_factor == 3.0
        assert cfg.anomaly_tighten_pct == 0.15
        assert cfg.anomaly_window_minutes == 10.0
        assert cfg.anomaly_recent_minutes == 5.0

    def test_shield_config_round_trip_anomaly(self):
        from veronica_core.shield.config import (
            AdaptiveBudgetConfig,
            ShieldConfig,
        )

        cfg = ShieldConfig(
            adaptive_budget=AdaptiveBudgetConfig(
                enabled=True,
                anomaly_enabled=True,
                anomaly_spike_factor=4.0,
                anomaly_tighten_pct=0.20,
                anomaly_window_minutes=15.0,
                anomaly_recent_minutes=3.0,
            )
        )
        d = cfg.to_dict()
        restored = ShieldConfig.from_dict(d)
        assert restored.adaptive_budget.anomaly_enabled is True
        assert restored.adaptive_budget.anomaly_spike_factor == 4.0
        assert restored.adaptive_budget.anomaly_tighten_pct == 0.20
        assert restored.adaptive_budget.anomaly_window_minutes == 15.0
        assert restored.adaptive_budget.anomaly_recent_minutes == 3.0


# ===========================================================================
# v0.7.0 Deterministic Replay API Tests
# ===========================================================================


# ---------------------------------------------------------------------------
# export_control_state
# ---------------------------------------------------------------------------


class TestExportControlState:
    def test_default_state(self):
        """Fresh hook exports clean default state."""
        hook = AdaptiveBudgetHook(base_ceiling=100)
        state = hook.export_control_state(_now=1000.0)
        assert state["adaptive_multiplier"] == 1.0
        assert state["time_multiplier"] == 1.0
        assert state["anomaly_factor"] == 1.0
        assert state["effective_multiplier"] == 1.0
        assert state["base_ceiling"] == 100
        assert state["adjusted_ceiling"] == 100
        assert state["hard_floor"] == pytest.approx(0.80)
        assert state["hard_ceiling"] == pytest.approx(1.20)
        assert state["last_adjustment_ts"] is None
        assert state["last_action"] is None
        assert state["cooldown_active"] is False
        assert state["cooldown_remaining_seconds"] is None
        assert state["anomaly_active"] is False
        assert state["anomaly_activated_ts"] is None
        assert state["direction_lock_active"] is False
        assert state["recent_event_counts"]["tighten"] == 0
        assert state["recent_event_counts"]["degrade"] == 0

    def test_after_tighten(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, tighten_trigger=3, tighten_pct=0.10
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1000.0)
        hook.adjust(_now=1000.0)
        state = hook.export_control_state(_now=1000.0)
        assert state["adaptive_multiplier"] == 0.90
        assert state["adjusted_ceiling"] == 90
        assert state["last_action"] == "tighten"
        assert state["last_adjustment_ts"] == 1000.0
        assert state["recent_event_counts"]["tighten"] == 3

    def test_with_cooldown_active(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, cooldown_seconds=60.0
        )
        hook.adjust(_now=1000.0)
        state = hook.export_control_state(_now=1030.0)
        assert state["cooldown_active"] is True
        assert state["cooldown_remaining_seconds"] == 30.0

    def test_with_cooldown_expired(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, cooldown_seconds=60.0
        )
        hook.adjust(_now=1000.0)
        state = hook.export_control_state(_now=1061.0)
        assert state["cooldown_active"] is False
        assert state["cooldown_remaining_seconds"] is None

    def test_with_time_multiplier(self):
        hook = AdaptiveBudgetHook(base_ceiling=100)
        state = hook.export_control_state(
            time_multiplier=0.85, _now=1000.0
        )
        assert state["time_multiplier"] == 0.85
        assert state["effective_multiplier"] == pytest.approx(0.85)
        assert state["adjusted_ceiling"] == 85

    def test_with_anomaly_active(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=600.0,
            tighten_trigger=3,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
            anomaly_tighten_pct=0.15,
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=550.0)
        hook.adjust(_now=550.0)
        state = hook.export_control_state(_now=550.0)
        assert state["anomaly_active"] is True
        assert state["anomaly_factor"] == 0.85
        assert state["anomaly_activated_ts"] == 550.0

    def test_with_direction_lock_active(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=100.0,
            tighten_trigger=3,
            direction_lock=True,
        )
        hook.feed_event(_halt_event(), ts=900.0)
        hook.feed_event(_halt_event(), ts=900.0)
        hook.feed_event(_halt_event(), ts=950.0)
        hook.adjust(_now=950.0)  # tighten
        state = hook.export_control_state(_now=1001.0)
        # 1 event remains at 950 (900s expired at 1001-100=901)
        assert state["direction_lock_active"] is True

    def test_json_serializable(self):
        """State dict must be JSON-serializable."""
        import json

        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            cooldown_seconds=60.0,
            direction_lock=True,
        )
        hook.adjust(_now=1000.0)
        state = hook.export_control_state(_now=1030.0)
        # Should not raise
        serialized = json.dumps(state)
        restored = json.loads(serialized)
        assert restored["adaptive_multiplier"] == state["adaptive_multiplier"]

    def test_combined_multipliers(self):
        """effective_multiplier compounds all three factors."""
        hook = AdaptiveBudgetHook(
            base_ceiling=1000,
            window_seconds=600.0,
            tighten_trigger=3,
            tighten_pct=0.10,
            anomaly_enabled=True,
            anomaly_spike_factor=2.0,
            anomaly_recent_seconds=100.0,
            anomaly_tighten_pct=0.15,
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=550.0)
        hook.adjust(_now=550.0)
        # adaptive=0.90, anomaly=0.85, time=0.90
        state = hook.export_control_state(
            time_multiplier=0.90, _now=550.0
        )
        expected = 0.90 * 0.90 * 0.85
        assert state["effective_multiplier"] == pytest.approx(
            expected, abs=0.001
        )
        assert state["adjusted_ceiling"] == max(
            1, round(1000 * expected)
        )


# ---------------------------------------------------------------------------
# import_control_state
# ---------------------------------------------------------------------------


class TestImportControlState:
    def test_round_trip(self):
        """Export then import restores core state."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            tighten_trigger=3,
            tighten_pct=0.10,
            cooldown_seconds=60.0,
        )
        for _ in range(3):
            hook.feed_event(_halt_event(), ts=1000.0)
        hook.adjust(_now=1000.0)

        state = hook.export_control_state(_now=1000.0)

        # Create fresh hook and import
        hook2 = AdaptiveBudgetHook(
            base_ceiling=100,
            tighten_trigger=3,
            tighten_pct=0.10,
            cooldown_seconds=60.0,
        )
        hook2.import_control_state(state)
        assert hook2.ceiling_multiplier == 0.90
        assert hook2.last_adjustment_ts == 1000.0
        assert hook2.last_action == "tighten"

    def test_import_with_anomaly(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            anomaly_enabled=True,
            anomaly_tighten_pct=0.15,
        )
        # Manually import anomaly state
        hook.import_control_state({
            "adaptive_multiplier": 0.85,
            "last_adjustment_ts": 500.0,
            "last_action": "tighten",
            "anomaly_active": True,
            "anomaly_activated_ts": 450.0,
        })
        assert hook.ceiling_multiplier == 0.85
        assert hook.anomaly_active is True
        assert hook.anomaly_activated_ts == 450.0
        # adjusted_ceiling includes anomaly: 100 * 0.85 * 0.85 = 72.25 -> 72
        assert hook.adjusted_ceiling == 72

    def test_import_resets_missing_fields(self):
        """Fields not in state dict get safe defaults."""
        hook = AdaptiveBudgetHook(base_ceiling=100)
        hook.import_control_state({
            "adaptive_multiplier": 0.95,
        })
        assert hook.ceiling_multiplier == 0.95
        assert hook.last_adjustment_ts is None
        assert hook.last_action is None
        assert hook.anomaly_active is False
        assert hook.anomaly_activated_ts is None

    def test_import_does_not_affect_events(self):
        """Import restores state but not event buffer or safety events."""
        hook = AdaptiveBudgetHook(base_ceiling=100)
        hook.adjust(_now=1000.0)  # creates an event
        assert len(hook.get_events()) == 1

        hook.import_control_state({
            "adaptive_multiplier": 0.80,
        })
        # Events should still be there
        assert len(hook.get_events()) == 1
        # But multiplier changed
        assert hook.ceiling_multiplier == 0.80


# ---------------------------------------------------------------------------
# from_dict forward compatibility
# ---------------------------------------------------------------------------


class TestFromDictForwardCompat:
    def test_unknown_keys_ignored(self):
        """from_dict ignores unknown keys for forward compatibility."""
        from veronica_core.shield.config import ShieldConfig

        data = {
            "adaptive_budget": {
                "enabled": True,
                "future_field": "some_value",
                "another_new_field": 42,
            },
            "safe_mode": {"enabled": False},
        }
        cfg = ShieldConfig.from_dict(data)
        assert cfg.adaptive_budget.enabled is True
        assert cfg.safe_mode.enabled is False

    def test_empty_dict_gives_defaults(self):
        from veronica_core.shield.config import ShieldConfig

        cfg = ShieldConfig.from_dict({})
        assert cfg.adaptive_budget.enabled is False
        assert cfg.is_any_enabled is False

    def test_none_sub_dict_gives_defaults(self):
        """None values in sub-dicts produce defaults (not AttributeError)."""
        from veronica_core.shield.config import ShieldConfig

        data = {"safe_mode": None, "adaptive_budget": None}
        cfg = ShieldConfig.from_dict(data)
        assert cfg.safe_mode.enabled is False
        assert cfg.adaptive_budget.enabled is False

    def test_round_trip_with_all_v07_fields(self):
        """Full round-trip preserves all v0.7.0 config fields."""
        from veronica_core.shield.config import (
            AdaptiveBudgetConfig,
            ShieldConfig,
            TimeAwarePolicyConfig,
        )

        cfg = ShieldConfig(
            adaptive_budget=AdaptiveBudgetConfig(
                enabled=True,
                cooldown_minutes=5.0,
                max_step_pct=0.02,
                min_multiplier=0.5,
                max_multiplier=1.5,
                direction_lock=False,
                anomaly_enabled=True,
                anomaly_spike_factor=4.0,
                anomaly_tighten_pct=0.20,
                anomaly_window_minutes=15.0,
                anomaly_recent_minutes=3.0,
            ),
            time_aware_policy=TimeAwarePolicyConfig(
                enabled=True,
                weekend_multiplier=0.70,
            ),
        )
        d = cfg.to_dict()
        restored = ShieldConfig.from_dict(d)
        assert restored.adaptive_budget.anomaly_enabled is True
        assert restored.adaptive_budget.direction_lock is False
        assert restored.adaptive_budget.anomaly_window_minutes == 15.0
        assert restored.time_aware_policy.weekend_multiplier == 0.70
