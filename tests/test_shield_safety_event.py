"""Tests for SafetyEvent and ShieldPipeline event recording."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from veronica_core.shield import (
    BudgetWindowHook,
    Decision,
    SafeModeHook,
    SafetyEvent,
    ShieldPipeline,
    ToolCallContext,
)

CTX = ToolCallContext(request_id="req-123", tool_name="bash")


class TestPipelineNoHooks:
    """Pipeline with no hooks produces no events."""

    def test_no_events_on_allow(self):
        pipe = ShieldPipeline()
        pipe.before_llm_call(CTX)
        assert pipe.get_events() == []

    def test_no_events_on_error(self):
        pipe = ShieldPipeline()
        pipe.on_error(CTX, RuntimeError("boom"))
        assert pipe.get_events() == []


class TestSafeModeEvent:
    """SafeModeHook fires → SafetyEvent with event_type='SAFE_MODE'."""

    def test_safe_mode_produces_event(self):
        hook = SafeModeHook(enabled=True)
        pipe = ShieldPipeline(pre_dispatch=hook)
        pipe.before_llm_call(CTX)

        events = pipe.get_events()
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "SAFE_MODE"
        assert e.decision is Decision.HALT
        assert e.hook == "SafeModeHook"
        assert e.request_id == "req-123"

    def test_safe_mode_on_error_produces_event(self):
        hook = SafeModeHook(enabled=True)
        pipe = ShieldPipeline(retry=hook)
        pipe.on_error(CTX, ValueError("err"))

        events = pipe.get_events()
        assert len(events) == 1
        assert events[0].event_type == "SAFE_MODE"
        assert events[0].decision is Decision.HALT

    def test_safe_mode_disabled_no_event(self):
        hook = SafeModeHook(enabled=False)
        pipe = ShieldPipeline(pre_dispatch=hook)
        pipe.before_llm_call(CTX)
        assert pipe.get_events() == []


class TestBudgetWindowEvent:
    """BudgetWindowHook fires → SafetyEvent with event_type='BUDGET_WINDOW_EXCEEDED'."""

    def test_budget_window_exceeded_produces_event(self):
        hook = BudgetWindowHook(max_calls=2, window_seconds=60.0)
        pipe = ShieldPipeline(pre_dispatch=hook)
        pipe.before_llm_call(CTX)  # call 1 - ALLOW
        pipe.before_llm_call(CTX)  # call 2 - ALLOW
        pipe.before_llm_call(CTX)  # call 3 - HALT

        events = pipe.get_events()
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "BUDGET_WINDOW_EXCEEDED"
        assert e.decision is Decision.HALT
        assert e.hook == "BudgetWindowHook"

    def test_budget_window_below_limit_no_event(self):
        hook = BudgetWindowHook(max_calls=10, window_seconds=60.0)
        pipe = ShieldPipeline(pre_dispatch=hook)
        for _ in range(5):
            pipe.before_llm_call(CTX)
        assert pipe.get_events() == []


class TestEventsAccumulate:
    """Multiple triggering calls accumulate multiple events."""

    def test_multiple_halts_accumulate(self):
        hook = BudgetWindowHook(max_calls=1, window_seconds=60.0)
        pipe = ShieldPipeline(pre_dispatch=hook)
        pipe.before_llm_call(CTX)  # ALLOW
        pipe.before_llm_call(CTX)  # HALT - event 1
        pipe.before_llm_call(CTX)  # HALT - event 2

        events = pipe.get_events()
        assert len(events) == 2
        assert all(e.decision is Decision.HALT for e in events)

    def test_pre_dispatch_and_retry_both_record(self):
        safe = SafeModeHook(enabled=True)
        pipe = ShieldPipeline(pre_dispatch=safe, retry=safe)
        pipe.before_llm_call(CTX)
        pipe.on_error(CTX, RuntimeError("x"))

        events = pipe.get_events()
        assert len(events) == 2


class TestClearEvents:
    """clear_events() empties the list."""

    def test_clear_events(self):
        hook = SafeModeHook(enabled=True)
        pipe = ShieldPipeline(pre_dispatch=hook)
        pipe.before_llm_call(CTX)
        assert len(pipe.get_events()) == 1

        pipe.clear_events()
        assert pipe.get_events() == []

    def test_clear_then_new_events_work(self):
        hook = SafeModeHook(enabled=True)
        pipe = ShieldPipeline(pre_dispatch=hook)
        pipe.before_llm_call(CTX)
        pipe.clear_events()
        pipe.before_llm_call(CTX)
        assert len(pipe.get_events()) == 1


class TestSafetyEventImmutable:
    """SafetyEvent is frozen (immutable)."""

    def test_frozen_dataclass(self):
        event = SafetyEvent(
            event_type="SAFE_MODE",
            decision=Decision.HALT,
            reason="test",
            hook="SafeModeHook",
        )
        with pytest.raises((AttributeError, TypeError)):
            event.event_type = "OTHER"  # type: ignore[misc]

    def test_metadata_default_empty(self):
        event = SafetyEvent(
            event_type="SAFE_MODE",
            decision=Decision.HALT,
            reason="test",
            hook="SafeModeHook",
        )
        assert event.metadata == {}


class TestEventCap:
    """Events list is capped at 1000 entries."""

    def test_record_event_capped_at_1000(self):
        hook = BudgetWindowHook(max_calls=0, window_seconds=60.0)
        pipe = ShieldPipeline(pre_dispatch=hook)
        for _ in range(1100):
            pipe.before_llm_call(CTX)
        assert len(pipe.get_events()) <= 1000


class TestSafetyEventTimestamp:
    """SafetyEvent timestamp is within 1 second of now."""

    def test_timestamp_is_recent(self):
        before = datetime.now(timezone.utc)
        event = SafetyEvent(
            event_type="TEST",
            decision=Decision.HALT,
            reason="ts test",
            hook="TestHook",
        )
        after = datetime.now(timezone.utc)

        assert before <= event.ts <= after

    def test_timestamp_is_utc(self):
        event = SafetyEvent(
            event_type="TEST",
            decision=Decision.HALT,
            reason="tz test",
            hook="TestHook",
        )
        assert event.ts.tzinfo is not None
        assert event.ts.tzinfo == timezone.utc
