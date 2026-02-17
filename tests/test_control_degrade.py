"""Tests for VERONICA Phase D: Degrade Strategy v0."""
from __future__ import annotations

import time

import pytest

from veronica.control.decision import (
    ControlSignals,
    Decision,
    DegradeConfig,
    DegradedRejected,
    DegradedToolBlocked,
    DegradeLevel,
    RequestMeta,
    SchedulerMode,
    compute_level,
    decide,
)
from veronica.control.controller import DegradeController, _ScopeState
from veronica.runtime.events import EventBus, Event


class CollectorSink:
    def __init__(self) -> None:
        self.events: list[Event] = []
    def emit(self, event: Event) -> None:
        self.events.append(event)


# --- compute_level tests (1-10) ---

def test_level_normal_no_signals():
    level, reasons = compute_level(ControlSignals(), DegradeConfig())
    assert level == DegradeLevel.NORMAL
    assert reasons == []

def test_level_soft_budget_80():
    signals = ControlSignals(budget_utilization=0.8)
    level, reasons = compute_level(signals, DegradeConfig())
    assert level == DegradeLevel.SOFT
    assert "budget_soft" in reasons

def test_level_hard_budget_90():
    signals = ControlSignals(budget_utilization=0.9)
    level, reasons = compute_level(signals, DegradeConfig())
    assert level == DegradeLevel.HARD
    assert "budget_hard" in reasons

def test_level_emergency_budget_98():
    signals = ControlSignals(budget_utilization=0.98)
    level, reasons = compute_level(signals, DegradeConfig())
    assert level == DegradeLevel.EMERGENCY
    assert "budget_emergency" in reasons

def test_level_soft_error_rate():
    signals = ControlSignals(recent_error_rate=0.3)
    level, reasons = compute_level(signals, DegradeConfig())
    assert level == DegradeLevel.SOFT
    assert "error_rate_elevated" in reasons

def test_level_hard_breaker_half_open():
    signals = ControlSignals(breaker_state="half_open")
    level, reasons = compute_level(signals, DegradeConfig())
    assert level == DegradeLevel.HARD
    assert "breaker_half_open" in reasons

def test_level_emergency_breaker_open():
    signals = ControlSignals(breaker_state="open")
    level, reasons = compute_level(signals, DegradeConfig())
    assert level == DegradeLevel.EMERGENCY
    assert "breaker_open" in reasons

def test_level_max_across_signals():
    """budget=80% (SOFT) + breaker=open (EMERGENCY) -> max = EMERGENCY"""
    signals = ControlSignals(budget_utilization=0.8, breaker_state="open")
    level, reasons = compute_level(signals, DegradeConfig())
    assert level == DegradeLevel.EMERGENCY
    assert "budget_soft" in reasons
    assert "breaker_open" in reasons

def test_level_soft_timeouts():
    signals = ControlSignals(recent_timeouts=3)
    level, reasons = compute_level(signals, DegradeConfig())
    assert level == DegradeLevel.SOFT
    assert "timeout_elevated" in reasons

def test_level_hard_consecutive_failures():
    signals = ControlSignals(consecutive_failures=5)
    level, reasons = compute_level(signals, DegradeConfig())
    assert level == DegradeLevel.HARD
    assert "consecutive_failures_high" in reasons


# --- decide() tests (11-22) ---

def test_decision_model_downgrade_level1():
    signals = ControlSignals(budget_utilization=0.8)
    request = RequestMeta(kind="llm_call", cheap_model="gpt-mini")
    d = decide(signals, request)
    assert d.level == DegradeLevel.SOFT
    assert d.model_override == "gpt-mini"

def test_decision_max_tokens_cap_level1():
    signals = ControlSignals(budget_utilization=0.8)
    request = RequestMeta(kind="llm_call", max_tokens=4096)
    d = decide(signals, request)
    assert d.max_tokens_cap == int(4096 * 0.7)  # 2867

def test_decision_max_tokens_cap_level2():
    signals = ControlSignals(budget_utilization=0.9)
    request = RequestMeta(kind="llm_call", max_tokens=4096)
    d = decide(signals, request)
    assert d.max_tokens_cap == int(4096 * 0.5)  # 2048

def test_decision_max_tokens_floor():
    signals = ControlSignals(budget_utilization=0.8)
    request = RequestMeta(kind="llm_call", max_tokens=100)
    d = decide(signals, request)
    # 100 * 0.7 = 70, but floor is 128
    assert d.max_tokens_cap == 128

def test_decision_tools_blocked_level2():
    signals = ControlSignals(budget_utilization=0.9)
    request = RequestMeta(kind="tool_call", tool_name="write_file")
    d = decide(signals, request)
    assert d.allow_tools is False
    assert d.allowed_tools == frozenset()

def test_decision_tools_readonly_level1():
    signals = ControlSignals(budget_utilization=0.8)
    ro = frozenset({"read_file", "search"})
    request = RequestMeta(kind="tool_call", tool_name="write_file", read_only_tools=ro)
    d = decide(signals, request)
    assert d.allow_tools is False
    assert d.allowed_tools == ro

def test_decision_llm_p0_allowed_level3():
    signals = ControlSignals(budget_utilization=0.98)
    request = RequestMeta(kind="llm_call", priority="P0")
    d = decide(signals, request)
    assert d.level == DegradeLevel.EMERGENCY
    assert d.allow_llm is True

def test_decision_llm_p1_blocked_level3():
    signals = ControlSignals(budget_utilization=0.98)
    request = RequestMeta(kind="llm_call", priority="P1")
    d = decide(signals, request)
    assert d.level == DegradeLevel.EMERGENCY
    assert d.allow_llm is False
    assert "non_p0_blocked" in d.reason_codes

def test_decision_retry_cap_level1():
    signals = ControlSignals(budget_utilization=0.8)
    request = RequestMeta(kind="llm_call")
    d = decide(signals, request)
    assert d.retry_cap_override == 2

def test_decision_retry_cap_level3():
    signals = ControlSignals(budget_utilization=0.98)
    request = RequestMeta(kind="llm_call", priority="P0")
    d = decide(signals, request)
    assert d.retry_cap_override == 0

def test_decision_scheduler_mode_level2():
    signals = ControlSignals(budget_utilization=0.9)
    request = RequestMeta(kind="llm_call")
    d = decide(signals, request)
    assert d.scheduler_mode == SchedulerMode.QUEUE_PREFER

def test_decision_scheduler_mode_level3():
    signals = ControlSignals(budget_utilization=0.98)
    request = RequestMeta(kind="llm_call", priority="P0")
    d = decide(signals, request)
    assert d.scheduler_mode == SchedulerMode.REJECT_PREFER


# --- DegradeController tests (23-27) ---

def test_controller_escalation_immediate():
    sink = CollectorSink()
    bus = EventBus([sink])
    ctrl = DegradeController(bus=bus)
    signals = ControlSignals(budget_utilization=0.9)
    request = RequestMeta(kind="llm_call")
    d = ctrl.evaluate("org1", "team1", signals, request, "run1")
    assert d.level == DegradeLevel.HARD
    assert ctrl.get_level("org1", "team1") == DegradeLevel.HARD

def test_controller_recovery_after_60s():
    """Simulate 60s stability -> recover one level."""
    ctrl = DegradeController(config=DegradeConfig(recovery_window_s=60.0))
    # First escalate to HARD
    signals_bad = ControlSignals(budget_utilization=0.9)
    request = RequestMeta(kind="llm_call")
    ctrl.evaluate("o", "t", signals_bad, request)
    assert ctrl.get_level("o", "t") == DegradeLevel.HARD

    # Now signals are good, but need to wait 60s
    signals_good = ControlSignals()
    ctrl.evaluate("o", "t", signals_good, request)
    assert ctrl.get_level("o", "t") == DegradeLevel.HARD  # Not recovered yet

    # Manipulate stable_since to simulate 60s passing
    state = ctrl._states[("o", "t")]
    state.stable_since = time.monotonic() - 61.0
    ctrl.evaluate("o", "t", signals_good, request)
    assert ctrl.get_level("o", "t") == DegradeLevel.SOFT  # Recovered by 1

def test_controller_recovery_one_step():
    """Level 3 -> 2 (not 3 -> 0) after recovery window."""
    ctrl = DegradeController(config=DegradeConfig(recovery_window_s=60.0))
    # Escalate to EMERGENCY
    signals_bad = ControlSignals(budget_utilization=0.98)
    request = RequestMeta(kind="llm_call", priority="P0")
    ctrl.evaluate("o", "t", signals_bad, request)
    assert ctrl.get_level("o", "t") == DegradeLevel.EMERGENCY

    # Simulate recovery
    signals_good = ControlSignals()
    ctrl.evaluate("o", "t", signals_good, request)
    state = ctrl._states[("o", "t")]
    state.stable_since = time.monotonic() - 61.0
    ctrl.evaluate("o", "t", signals_good, request)
    # Should be HARD (3->2), NOT NORMAL (3->0)
    assert ctrl.get_level("o", "t") == DegradeLevel.HARD

def test_controller_feed_result_tracking():
    ctrl = DegradeController()
    assert ctrl.get_consecutive_failures("o", "t") == 0
    ctrl.feed_result("o", "t", success=False)
    assert ctrl.get_consecutive_failures("o", "t") == 1
    ctrl.feed_result("o", "t", success=False)
    assert ctrl.get_consecutive_failures("o", "t") == 2
    ctrl.feed_result("o", "t", success=True)
    assert ctrl.get_consecutive_failures("o", "t") == 0

def test_controller_events_emitted():
    sink = CollectorSink()
    bus = EventBus([sink])
    ctrl = DegradeController(bus=bus)
    signals = ControlSignals(budget_utilization=0.8)
    request = RequestMeta(kind="llm_call")
    ctrl.evaluate("org1", "team1", signals, request, "run1")
    # Should have emitted level_changed and decision_made
    types = [e.type for e in sink.events]
    assert "control.degrade.level_changed" in types
    assert "control.decision.made" in types
