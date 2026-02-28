"""Tests for TimeAwarePolicy (v0.6.0)."""

from __future__ import annotations

import threading
from datetime import datetime, time, timezone

import pytest

from veronica_core.shield.time_policy import TimeAwarePolicy, TimeResult
from veronica_core.shield.types import Decision, ToolCallContext

CTX = ToolCallContext(request_id="test-time", tool_name="llm")


def _dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Create a UTC datetime for testing."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# Known dates:
# 2026-02-16 = Monday, 2026-02-17 = Tuesday, 2026-02-21 = Saturday, 2026-02-22 = Sunday
MONDAY_10AM = _dt(2026, 2, 16, 10)
MONDAY_7AM = _dt(2026, 2, 16, 7)
MONDAY_19PM = _dt(2026, 2, 16, 19)
MONDAY_23PM = _dt(2026, 2, 16, 23)
SATURDAY_14PM = _dt(2026, 2, 21, 14)
SATURDAY_3AM = _dt(2026, 2, 21, 3)
SUNDAY_10AM = _dt(2026, 2, 22, 10)
SUNDAY_22PM = _dt(2026, 2, 22, 22)


# ---------------------------------------------------------------------------
# Construction & validation
# ---------------------------------------------------------------------------


class TestInit:
    def test_defaults(self):
        policy = TimeAwarePolicy()
        assert policy.weekend_multiplier == 0.85
        assert policy.offhour_multiplier == 0.90
        assert policy.work_start == time(9, 0)
        assert policy.work_end == time(18, 0)

    def test_custom_params(self):
        policy = TimeAwarePolicy(
            weekend_multiplier=0.70,
            offhour_multiplier=0.80,
            work_start=time(8, 30),
            work_end=time(17, 30),
        )
        assert policy.weekend_multiplier == 0.70
        assert policy.offhour_multiplier == 0.80
        assert policy.work_start == time(8, 30)
        assert policy.work_end == time(17, 30)

    def test_validates_weekend_multiplier_zero(self):
        with pytest.raises(ValueError, match="weekend_multiplier"):
            TimeAwarePolicy(weekend_multiplier=0.0)

    def test_validates_weekend_multiplier_over_one(self):
        with pytest.raises(ValueError, match="weekend_multiplier"):
            TimeAwarePolicy(weekend_multiplier=1.5)

    def test_validates_offhour_multiplier(self):
        with pytest.raises(ValueError, match="offhour_multiplier"):
            TimeAwarePolicy(offhour_multiplier=0.0)

    def test_validates_work_hours_order(self):
        with pytest.raises(ValueError, match="work_start.*before.*work_end"):
            TimeAwarePolicy(work_start=time(18, 0), work_end=time(9, 0))

    def test_validates_work_hours_equal(self):
        with pytest.raises(ValueError, match="work_start.*before.*work_end"):
            TimeAwarePolicy(work_start=time(9, 0), work_end=time(9, 0))


# ---------------------------------------------------------------------------
# get_multiplier: business hours
# ---------------------------------------------------------------------------


class TestBusinessHours:
    def test_weekday_business_hours(self):
        policy = TimeAwarePolicy()
        assert policy.get_multiplier(MONDAY_10AM) == 1.0

    def test_weekday_at_work_start(self):
        policy = TimeAwarePolicy()
        dt = _dt(2026, 2, 16, 9, 0)  # Monday 09:00
        assert policy.get_multiplier(dt) == 1.0

    def test_weekday_just_before_work_end(self):
        policy = TimeAwarePolicy()
        dt = _dt(2026, 2, 16, 17, 59)  # Monday 17:59
        assert policy.get_multiplier(dt) == 1.0


# ---------------------------------------------------------------------------
# get_multiplier: off-hours
# ---------------------------------------------------------------------------


class TestOffHours:
    def test_weekday_before_work(self):
        policy = TimeAwarePolicy()
        assert policy.get_multiplier(MONDAY_7AM) == 0.90

    def test_weekday_after_work(self):
        policy = TimeAwarePolicy()
        assert policy.get_multiplier(MONDAY_19PM) == 0.90

    def test_weekday_at_work_end(self):
        policy = TimeAwarePolicy()
        dt = _dt(2026, 2, 16, 18, 0)  # Monday 18:00 = off-hours
        assert policy.get_multiplier(dt) == 0.90


# ---------------------------------------------------------------------------
# get_multiplier: weekends
# ---------------------------------------------------------------------------


class TestWeekend:
    def test_saturday_business_hours(self):
        policy = TimeAwarePolicy()
        assert policy.get_multiplier(SATURDAY_14PM) == 0.85

    def test_sunday_business_hours(self):
        policy = TimeAwarePolicy()
        assert policy.get_multiplier(SUNDAY_10AM) == 0.85


# ---------------------------------------------------------------------------
# get_multiplier: weekend + off-hours
# ---------------------------------------------------------------------------


class TestWeekendOffHours:
    def test_saturday_offhour(self):
        policy = TimeAwarePolicy()
        # Weekend AND off-hours -> min(0.85, 0.90) = 0.85
        assert policy.get_multiplier(SATURDAY_3AM) == 0.85

    def test_weekend_offhour_custom_multipliers(self):
        policy = TimeAwarePolicy(
            weekend_multiplier=0.70, offhour_multiplier=0.60
        )
        # min(0.70, 0.60) = 0.60
        assert policy.get_multiplier(SATURDAY_3AM) == 0.60


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_business_hours_no_event(self):
        policy = TimeAwarePolicy()
        result = policy.evaluate(CTX, dt=MONDAY_10AM)
        assert result.multiplier == 1.0
        assert result.classification == "business_hours"
        assert result.is_weekend is False
        assert result.is_offhour is False
        assert policy.get_events() == []

    def test_offhour_records_event(self):
        policy = TimeAwarePolicy()
        result = policy.evaluate(CTX, dt=MONDAY_7AM)
        assert result.multiplier == 0.90
        assert result.classification == "offhour"
        events = policy.get_events()
        assert len(events) == 1
        assert events[0].event_type == "TIME_POLICY_APPLIED"
        assert events[0].decision == Decision.DEGRADE

    def test_weekend_records_event(self):
        policy = TimeAwarePolicy()
        result = policy.evaluate(CTX, dt=SATURDAY_14PM)
        assert result.multiplier == 0.85
        assert result.classification == "weekend"
        events = policy.get_events()
        assert len(events) == 1
        assert events[0].metadata["classification"] == "weekend"

    def test_weekend_offhour_records_event(self):
        policy = TimeAwarePolicy()
        result = policy.evaluate(CTX, dt=SATURDAY_3AM)
        assert result.classification == "weekend_offhour"
        events = policy.get_events()
        assert len(events) == 1
        assert events[0].metadata["is_weekend"] is True
        assert events[0].metadata["is_offhour"] is True

    def test_event_has_request_id(self):
        policy = TimeAwarePolicy()
        policy.evaluate(CTX, dt=MONDAY_7AM)
        assert policy.get_events()[0].request_id == "test-time"

    def test_event_without_ctx(self):
        policy = TimeAwarePolicy()
        policy.evaluate(dt=MONDAY_7AM)
        assert policy.get_events()[0].request_id is None

    def test_event_metadata_fields(self):
        policy = TimeAwarePolicy()
        policy.evaluate(CTX, dt=MONDAY_7AM)
        meta = policy.get_events()[0].metadata
        assert "multiplier" in meta
        assert "classification" in meta
        assert "is_weekend" in meta
        assert "is_offhour" in meta
        assert "time" in meta
        assert "weekday" in meta


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


class TestStateManagement:
    def test_get_events_returns_copy(self):
        policy = TimeAwarePolicy()
        policy.evaluate(dt=MONDAY_7AM)
        events1 = policy.get_events()
        events2 = policy.get_events()
        assert events1 is not events2

    def test_clear_events(self):
        policy = TimeAwarePolicy()
        policy.evaluate(dt=MONDAY_7AM)
        assert len(policy.get_events()) == 1
        policy.clear_events()
        assert len(policy.get_events()) == 0

    def test_multiple_evaluations_accumulate(self):
        policy = TimeAwarePolicy()
        policy.evaluate(dt=MONDAY_7AM)
        policy.evaluate(dt=SATURDAY_14PM)
        assert len(policy.get_events()) == 2


# ---------------------------------------------------------------------------
# Custom work hours
# ---------------------------------------------------------------------------


class TestCustomWorkHours:
    def test_early_start(self):
        policy = TimeAwarePolicy(work_start=time(6, 0), work_end=time(15, 0))
        dt = _dt(2026, 2, 16, 6, 0)  # Monday 06:00
        assert policy.get_multiplier(dt) == 1.0

    def test_late_end(self):
        policy = TimeAwarePolicy(work_start=time(10, 0), work_end=time(22, 0))
        dt = _dt(2026, 2, 16, 21, 59)  # Monday 21:59
        assert policy.get_multiplier(dt) == 1.0

    def test_narrow_window(self):
        policy = TimeAwarePolicy(work_start=time(10, 0), work_end=time(12, 0))
        # 09:59 = offhour
        assert policy.get_multiplier(_dt(2026, 2, 16, 9, 59)) == 0.90
        # 10:00 = business
        assert policy.get_multiplier(_dt(2026, 2, 16, 10, 0)) == 1.0
        # 11:59 = business
        assert policy.get_multiplier(_dt(2026, 2, 16, 11, 59)) == 1.0
        # 12:00 = offhour
        assert policy.get_multiplier(_dt(2026, 2, 16, 12, 0)) == 0.90


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_evaluate(self):
        policy = TimeAwarePolicy()
        errors: list[Exception] = []

        def evaluate_loop():
            try:
                for _ in range(50):
                    policy.evaluate(dt=MONDAY_7AM)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=evaluate_loop) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(policy.get_events()) == 200  # 4 threads x 50


# ---------------------------------------------------------------------------
# Config round-trip
# ---------------------------------------------------------------------------


class TestConfigRoundTrip:
    def test_config_defaults(self):
        from veronica_core.shield.config import TimeAwarePolicyConfig

        cfg = TimeAwarePolicyConfig()
        assert cfg.enabled is False
        assert cfg.weekend_multiplier == 0.85
        assert cfg.offhour_multiplier == 0.90
        assert cfg.work_start_hour == 9
        assert cfg.work_end_hour == 18

    def test_shield_config_round_trip(self):
        from veronica_core.shield.config import (
            ShieldConfig,
            TimeAwarePolicyConfig,
        )

        cfg = ShieldConfig(
            time_aware_policy=TimeAwarePolicyConfig(
                enabled=True,
                weekend_multiplier=0.70,
                offhour_multiplier=0.80,
            )
        )
        d = cfg.to_dict()
        restored = ShieldConfig.from_dict(d)
        assert restored.time_aware_policy.enabled is True
        assert restored.time_aware_policy.weekend_multiplier == 0.70
        assert restored.time_aware_policy.offhour_multiplier == 0.80

    def test_shield_config_is_any_enabled(self):
        from veronica_core.shield.config import (
            ShieldConfig,
            TimeAwarePolicyConfig,
        )

        cfg = ShieldConfig(
            time_aware_policy=TimeAwarePolicyConfig(enabled=True)
        )
        assert cfg.is_any_enabled is True


# ---------------------------------------------------------------------------
# TimeResult
# ---------------------------------------------------------------------------


class TestTimeResult:
    def test_result_fields(self):
        result = TimeResult(
            multiplier=0.85,
            classification="weekend",
            is_weekend=True,
            is_offhour=False,
        )
        assert result.multiplier == 0.85
        assert result.classification == "weekend"
        assert result.is_weekend is True
        assert result.is_offhour is False
