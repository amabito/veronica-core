"""Tests for AdaptiveBudgetHook (v0.6.0)."""

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
