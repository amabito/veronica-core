"""Tests for R1 (max_steps enforcement) and R2 (loop_detection_on flag)."""
from __future__ import annotations

import pytest

from veronica.runtime.events import Event, EventBus, EventTypes
from veronica.runtime.hooks import MaxStepsExceeded, RuntimeContext
from veronica.runtime.models import SessionStatus


class CollectorSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


# --- R1: max_steps enforcement ---


def test_max_steps_llm_call_blocks_at_limit():
    """max_steps=2: two llm_calls succeed, third raises MaxStepsExceeded."""
    ctx = RuntimeContext(sinks=[])
    run = ctx.create_run()
    session = ctx.create_session(run, max_steps=2)

    with ctx.llm_call(session, model="gpt-4o") as step:
        step.cost_usd = 0.01
    assert session.counters.steps_total == 1

    with ctx.llm_call(session, model="gpt-4o") as step:
        step.cost_usd = 0.01
    assert session.counters.steps_total == 2

    with pytest.raises(MaxStepsExceeded) as exc_info:
        with ctx.llm_call(session, model="gpt-4o") as step:
            step.cost_usd = 0.01

    assert exc_info.value.steps_executed == 2
    assert exc_info.value.max_steps == 2
    assert session.status == SessionStatus.HALTED


def test_max_steps_tool_call_blocks_at_limit():
    """max_steps=1: first tool_call succeeds, second raises MaxStepsExceeded."""
    ctx = RuntimeContext(sinks=[])
    run = ctx.create_run()
    session = ctx.create_session(run, max_steps=1)

    with ctx.tool_call(session, tool_name="search") as step:
        pass
    assert session.counters.steps_total == 1

    with pytest.raises(MaxStepsExceeded) as exc_info:
        with ctx.tool_call(session, tool_name="search") as step:
            pass

    assert exc_info.value.steps_executed == 1
    assert exc_info.value.max_steps == 1
    assert session.status == SessionStatus.HALTED


def test_max_steps_mixed_llm_and_tool():
    """max_steps counts both llm_call and tool_call steps."""
    ctx = RuntimeContext(sinks=[])
    run = ctx.create_run()
    session = ctx.create_session(run, max_steps=2)

    with ctx.llm_call(session, model="gpt-4o") as step:
        step.cost_usd = 0.01

    with ctx.tool_call(session, tool_name="search") as step:
        pass

    assert session.counters.steps_total == 2

    # Third step (either kind) must be blocked
    with pytest.raises(MaxStepsExceeded):
        with ctx.llm_call(session, model="gpt-4o") as step:
            pass


def test_max_steps_zero_means_unlimited():
    """max_steps=0 means no limit (unlimited steps)."""
    ctx = RuntimeContext(sinks=[])
    run = ctx.create_run()
    session = ctx.create_session(run, max_steps=0)

    for _ in range(150):
        with ctx.llm_call(session, model="gpt-4o") as step:
            step.cost_usd = 0.001

    assert session.counters.steps_total == 150
    assert session.status == SessionStatus.RUNNING


def test_max_steps_failed_steps_count():
    """Failed steps also count toward max_steps."""
    ctx = RuntimeContext(sinks=[])
    run = ctx.create_run()
    session = ctx.create_session(run, max_steps=1)

    # A step that raises counts as a completed step
    with pytest.raises(ValueError):
        with ctx.llm_call(session, model="gpt-4o") as step:
            raise ValueError("simulated failure")

    assert session.counters.steps_total == 1

    # Next step is blocked
    with pytest.raises(MaxStepsExceeded):
        with ctx.llm_call(session, model="gpt-4o") as step:
            pass


def test_max_steps_event_emitted():
    """MAX_STEPS_EXCEEDED event is emitted with correct payload."""
    sink = CollectorSink()
    ctx = RuntimeContext(sinks=[sink])
    run = ctx.create_run()
    session = ctx.create_session(run, max_steps=1)

    with ctx.llm_call(session, model="gpt-4o") as step:
        step.cost_usd = 0.01

    with pytest.raises(MaxStepsExceeded):
        with ctx.llm_call(session, model="gpt-4o") as step:
            pass

    max_steps_events = [e for e in sink.events if e.type == EventTypes.MAX_STEPS_EXCEEDED]
    assert len(max_steps_events) == 1

    evt = max_steps_events[0]
    assert evt.session_id == session.session_id
    assert evt.run_id == run.run_id
    assert evt.payload["steps_executed"] == 1
    assert evt.payload["max_steps"] == 1


def test_max_steps_session_halted_before_event():
    """Session transitions to HALTED before the event is emitted."""
    events_at_halt: list[SessionStatus] = []

    class StatusCaptureSink:
        def __init__(self, session_ref):
            self._session = session_ref

        def emit(self, event: Event) -> None:
            if event.type == EventTypes.MAX_STEPS_EXCEEDED:
                events_at_halt.append(self._session.status)

    ctx_inner = RuntimeContext(sinks=[])
    run = ctx_inner.create_run()
    session = ctx_inner.create_session(run, max_steps=1)

    # Rebuild context with the capture sink now that session exists
    capture_sink = StatusCaptureSink(session)
    ctx = RuntimeContext(sinks=[capture_sink])
    # Re-use the same session/run objects
    with ctx.llm_call(session, model="gpt-4o") as step:
        step.cost_usd = 0.01

    with pytest.raises(MaxStepsExceeded):
        with ctx.llm_call(session, model="gpt-4o") as step:
            pass

    assert events_at_halt == [SessionStatus.HALTED]


def test_max_steps_repeated_calls_after_halt():
    """Subsequent calls after HALTED also raise MaxStepsExceeded."""
    ctx = RuntimeContext(sinks=[])
    run = ctx.create_run()
    session = ctx.create_session(run, max_steps=1)

    with ctx.llm_call(session, model="gpt-4o") as step:
        step.cost_usd = 0.01

    # First exceed
    with pytest.raises(MaxStepsExceeded):
        with ctx.llm_call(session, model="gpt-4o") as step:
            pass

    # Second exceed (session already HALTED, should still raise)
    with pytest.raises(MaxStepsExceeded):
        with ctx.tool_call(session, tool_name="search") as step:
            pass


# --- R2: loop_detection_on flag ---


def test_loop_detection_on_true_halts():
    """When loop_detection_on=True (default), record_loop_detected() halts."""
    ctx = RuntimeContext(sinks=[])
    run = ctx.create_run()
    session = ctx.create_session(run, loop_detection_on=True)

    ctx.record_loop_detected(session, details="repeated failure")

    assert session.status == SessionStatus.HALTED


def test_loop_detection_on_false_noop():
    """When loop_detection_on=False, record_loop_detected() is a no-op."""
    sink = CollectorSink()
    ctx = RuntimeContext(sinks=[sink])
    run = ctx.create_run()
    session = ctx.create_session(run, loop_detection_on=False)

    ctx.record_loop_detected(session, details="repeated failure")

    assert session.status == SessionStatus.RUNNING
    loop_events = [e for e in sink.events if e.type == EventTypes.LOOP_DETECTED]
    assert len(loop_events) == 0


def test_loop_detection_on_false_no_event():
    """When loop_detection_on=False, no LOOP_DETECTED event is emitted."""
    sink = CollectorSink()
    ctx = RuntimeContext(sinks=[sink])
    run = ctx.create_run()
    session = ctx.create_session(run, loop_detection_on=False)

    ctx.record_loop_detected(session, details="test")

    event_types = [e.type for e in sink.events]
    assert EventTypes.LOOP_DETECTED not in event_types


def test_loop_detection_default_is_on():
    """Default loop_detection_on=True: record_loop_detected halts."""
    ctx = RuntimeContext(sinks=[])
    run = ctx.create_run()
    session = ctx.create_session(run)  # default loop_detection_on=True

    assert session.loop_detection_on is True
    ctx.record_loop_detected(session, details="test")
    assert session.status == SessionStatus.HALTED
