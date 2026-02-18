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
    def test_defaults(self):
        hook = AdaptiveBudgetHook(base_ceiling=100)
        assert hook.base_ceiling == 100
        assert hook.ceiling_multiplier == 1.0
        assert hook.adjusted_ceiling == 100
        assert hook.window_seconds == 1800.0

    def test_custom_params(self):
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

    def test_validates_base_ceiling_positive(self):
        with pytest.raises(ValueError, match="base_ceiling must be positive"):
            AdaptiveBudgetHook(base_ceiling=0)

    def test_validates_base_ceiling_negative(self):
        with pytest.raises(ValueError, match="base_ceiling must be positive"):
            AdaptiveBudgetHook(base_ceiling=-10)

    def test_validates_tighten_pct(self):
        with pytest.raises(ValueError, match="tighten_pct"):
            AdaptiveBudgetHook(base_ceiling=100, tighten_pct=0.0)

    def test_validates_loosen_pct(self):
        with pytest.raises(ValueError, match="loosen_pct"):
            AdaptiveBudgetHook(base_ceiling=100, loosen_pct=-0.1)

    def test_validates_max_adjustment(self):
        with pytest.raises(ValueError, match="max_adjustment"):
            AdaptiveBudgetHook(base_ceiling=100, max_adjustment=0.0)


# ---------------------------------------------------------------------------
# Event ingestion
# ---------------------------------------------------------------------------


class TestFeedEvents:
    def test_feed_single_event(self):
        hook = AdaptiveBudgetHook(base_ceiling=100)
        event = _halt_event()
        hook.feed_event(event, ts=1000.0)
        # No crash, event is buffered internally

    def test_feed_multiple_events(self):
        hook = AdaptiveBudgetHook(base_ceiling=100)
        events = [_halt_event() for _ in range(5)]
        hook.feed_events(events)
        # No crash, 5 events buffered


# ---------------------------------------------------------------------------
# Adjust: hold
# ---------------------------------------------------------------------------


class TestAdjustHold:
    def test_hold_when_degrade_events_exist(self):
        """Some degrade but below tighten threshold -> hold."""
        hook = AdaptiveBudgetHook(base_ceiling=100, tighten_trigger=3)
        # Feed 1 HALT (below trigger) + 1 DEGRADE (prevents loosen)
        hook.feed_event(_halt_event(), ts=1000.0)
        hook.feed_event(_degrade_event(), ts=1000.0)

        result = hook.adjust(_now=1000.0)
        assert result.action == "hold"
        assert result.adjusted_ceiling == 100
        assert result.ceiling_multiplier == 1.0

    def test_hold_records_no_event(self):
        hook = AdaptiveBudgetHook(base_ceiling=100, tighten_trigger=3)
        hook.feed_event(_halt_event(), ts=1000.0)
        hook.feed_event(_degrade_event(), ts=1000.0)
        hook.adjust(_now=1000.0)
        assert hook.get_events() == []


# ---------------------------------------------------------------------------
# Adjust: tighten
# ---------------------------------------------------------------------------


class TestAdjustTighten:
    def test_tighten_on_exceeded_events(self):
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

    def test_tighten_clamp_at_min(self):
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

    def test_multiple_tighten_adjustments(self):
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

    def test_tighten_with_token_budget_exceeded(self):
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
    def test_loosen_when_no_degrade(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, loosen_pct=0.05
        )
        # Empty window -> no degrade events -> loosen
        result = hook.adjust(_now=1000.0)
        assert result.action == "loosen"
        assert result.ceiling_multiplier == 1.05
        assert result.adjusted_ceiling == 105

    def test_loosen_records_allow_event(self):
        hook = AdaptiveBudgetHook(base_ceiling=100, loosen_pct=0.05)
        hook.adjust(CTX, _now=1000.0)
        events = hook.get_events()
        assert len(events) == 1
        assert events[0].event_type == "ADAPTIVE_ADJUSTMENT"
        assert events[0].decision == Decision.ALLOW

    def test_loosen_clamp_at_max(self):
        hook = AdaptiveBudgetHook(
            base_ceiling=100, loosen_pct=0.15, max_adjustment=0.20
        )
        # 3 loosen cycles: 1.0 -> 1.15 -> 1.20 (clamped at 1.20)
        for i in range(3):
            hook.adjust(_now=1000.0 + i)

        assert hook.ceiling_multiplier == 1.20
        assert hook.adjusted_ceiling == 120

    def test_loosen_after_empty_window(self):
        """No events at all -> zero degrade -> loosen."""
        hook = AdaptiveBudgetHook(base_ceiling=100, loosen_pct=0.05)
        result = hook.adjust(_now=5000.0)
        assert result.action == "loosen"
        assert result.degrade_events_in_window == 0


# ---------------------------------------------------------------------------
# Window expiry
# ---------------------------------------------------------------------------


class TestWindowExpiry:
    def test_old_events_pruned(self):
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
