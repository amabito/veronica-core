"""Tests for VERONICA Runtime Phase A — Run/Session/Step + events."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from veronica.runtime.events import EventTypes
from veronica.runtime.hooks import RuntimeContext
from veronica.runtime.models import (
    Budget,
    Labels,
    RunStatus,
    SessionStatus,
    StepStatus,
    make_result_ref,
)
from veronica.runtime.sinks import JsonlFileSink, NullSink, create_default_sinks
from veronica.runtime.state_machine import InvalidTransitionError


def _read_events(path: Path) -> list[dict]:
    """Read all events from a JSONL file."""
    events = []
    if not path.exists():
        return events
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _make_ctx(tmp_path: Path) -> tuple[RuntimeContext, Path]:
    """Create a RuntimeContext with a JsonlFileSink writing to tmp_path."""
    jsonl_path = tmp_path / "events.jsonl"
    sink = JsonlFileSink(jsonl_path)
    ctx = RuntimeContext(sinks=[sink])
    return ctx, jsonl_path


# --- Test 1: Run lifecycle ---

def test_run_lifecycle(tmp_path: Path) -> None:
    ctx, jsonl_path = _make_ctx(tmp_path)
    run = ctx.create_run(labels=Labels(org="test-org", service="test-svc"))
    assert run.status == RunStatus.RUNNING

    ctx.finish_run(run, RunStatus.SUCCEEDED)
    assert run.status == RunStatus.SUCCEEDED
    assert run.finished_at is not None

    events = _read_events(jsonl_path)
    types = [e["type"] for e in events]
    assert EventTypes.RUN_CREATED in types
    assert EventTypes.RUN_STATE_CHANGED in types
    assert EventTypes.RUN_FINISHED in types


# --- Test 2: Session lifecycle ---

def test_session_lifecycle(tmp_path: Path) -> None:
    ctx, jsonl_path = _make_ctx(tmp_path)
    run = ctx.create_run()
    session = ctx.create_session(run, agent_name="test-agent", max_steps=50)
    assert session.status == SessionStatus.RUNNING
    assert session.run_id == run.run_id

    ctx.finish_session(session, SessionStatus.SUCCEEDED)
    assert session.status == SessionStatus.SUCCEEDED

    events = _read_events(jsonl_path)
    types = [e["type"] for e in events]
    assert EventTypes.SESSION_CREATED in types
    assert EventTypes.SESSION_FINISHED in types


# --- Test 3: LLM call success ---

def test_step_llm_call_success(tmp_path: Path) -> None:
    ctx, jsonl_path = _make_ctx(tmp_path)
    run = ctx.create_run()
    session = ctx.create_session(run, agent_name="llm-test")

    with ctx.llm_call(session, model="gpt-4", provider="openai") as step:
        step.tokens_in = 100
        step.tokens_out = 50
        step.cost_usd = 0.003

    assert step.status == StepStatus.SUCCEEDED
    assert step.latency_ms is not None
    assert session.counters.llm_calls == 1
    assert session.counters.steps_total == 1

    events = _read_events(jsonl_path)
    types = [e["type"] for e in events]
    assert EventTypes.LLM_CALL_STARTED in types
    assert EventTypes.LLM_CALL_SUCCEEDED in types
    assert EventTypes.STEP_STARTED in types
    assert EventTypes.STEP_SUCCEEDED in types


# --- Test 4: LLM call failure ---

def test_step_llm_call_failure(tmp_path: Path) -> None:
    ctx, jsonl_path = _make_ctx(tmp_path)
    run = ctx.create_run()
    session = ctx.create_session(run, agent_name="llm-fail")

    with pytest.raises(ValueError, match="test error"):
        with ctx.llm_call(session, model="gpt-4") as step:
            raise ValueError("test error")

    assert step.status == StepStatus.FAILED
    assert step.error is not None
    assert step.error.type == "ValueError"

    events = _read_events(jsonl_path)
    types = [e["type"] for e in events]
    assert EventTypes.LLM_CALL_FAILED in types
    assert EventTypes.STEP_FAILED in types


# --- Test 5: Tool call ---

def test_step_tool_call(tmp_path: Path) -> None:
    ctx, jsonl_path = _make_ctx(tmp_path)
    run = ctx.create_run()
    session = ctx.create_session(run, agent_name="tool-test")

    with ctx.tool_call(session, tool_name="web_search") as step:
        step.result_ref = "abc123:search results preview"

    assert step.status == StepStatus.SUCCEEDED
    assert session.counters.tool_calls == 1

    events = _read_events(jsonl_path)
    types = [e["type"] for e in events]
    assert EventTypes.TOOL_CALL_STARTED in types
    assert EventTypes.TOOL_CALL_SUCCEEDED in types


# --- Test 6: Budget exceeded ---

def test_budget_exceeded(tmp_path: Path) -> None:
    ctx, jsonl_path = _make_ctx(tmp_path)
    run = ctx.create_run(budget=Budget(limit_usd=1.0, used_usd=0.0))

    # Simulate spending over budget
    run.budget.used_usd = 1.5
    exceeded = ctx.check_budget(run)

    assert exceeded is True
    assert run.status == RunStatus.HALTED

    events = _read_events(jsonl_path)
    types = [e["type"] for e in events]
    assert EventTypes.BUDGET_CHECK in types
    assert EventTypes.BUDGET_EXCEEDED in types
    assert EventTypes.RUN_STATE_CHANGED in types


# --- Test 7: Loop detected ---

def test_loop_detected(tmp_path: Path) -> None:
    ctx, jsonl_path = _make_ctx(tmp_path)
    run = ctx.create_run()
    session = ctx.create_session(run, agent_name="loop-test")

    ctx.record_loop_detected(session, details="repeated pattern x3")

    assert session.status == SessionStatus.HALTED

    events = _read_events(jsonl_path)
    types = [e["type"] for e in events]
    assert EventTypes.LOOP_DETECTED in types


# --- Test 8: Timeout triggered ---

def test_timeout_triggered(tmp_path: Path) -> None:
    ctx, jsonl_path = _make_ctx(tmp_path)
    run = ctx.create_run()
    session = ctx.create_session(run, agent_name="timeout-test")

    ctx.trigger_timeout(session, reason="exceeded 30s limit")

    assert session.status == SessionStatus.HALTED

    events = _read_events(jsonl_path)
    types = [e["type"] for e in events]
    assert EventTypes.TIMEOUT_TRIGGERED in types


# --- Test 9: Abort triggered ---

def test_abort_triggered(tmp_path: Path) -> None:
    ctx, jsonl_path = _make_ctx(tmp_path)
    run = ctx.create_run()

    ctx.trigger_abort(run, reason="user requested abort")

    assert run.status == RunStatus.HALTED

    events = _read_events(jsonl_path)
    types = [e["type"] for e in events]
    assert EventTypes.ABORT_TRIGGERED in types
    assert EventTypes.RUN_STATE_CHANGED in types


# --- Test 10: Breaker transitions ---

def test_breaker_transitions(tmp_path: Path) -> None:
    ctx, jsonl_path = _make_ctx(tmp_path)
    run = ctx.create_run()

    ctx.record_breaker_change(run, "open", reason="3 consecutive failures")
    assert run.status == RunStatus.DEGRADED

    ctx.record_breaker_change(run, "half_open", reason="cooldown elapsed")
    ctx.record_breaker_change(run, "closed", reason="probe succeeded")

    events = _read_events(jsonl_path)
    types = [e["type"] for e in events]
    assert EventTypes.BREAKER_OPENED in types
    assert EventTypes.BREAKER_HALF_OPEN in types
    assert EventTypes.BREAKER_CLOSED in types


# --- Test 11: Retry scheduled and exhausted ---

def test_retry_scheduled_and_exhausted(tmp_path: Path) -> None:
    ctx, jsonl_path = _make_ctx(tmp_path)
    run = ctx.create_run()
    session = ctx.create_session(run, agent_name="retry-test")

    # Create a dummy step for retry tracking
    with ctx.llm_call(session, model="gpt-4") as step:
        pass  # step succeeds

    # Record retries
    ctx.record_retry(session, step, attempt=1, max_attempts=3)
    ctx.record_retry(session, step, attempt=2, max_attempts=3)
    ctx.record_retry(session, step, attempt=3, max_attempts=3)

    assert session.counters.retries_total == 3

    events = _read_events(jsonl_path)
    types = [e["type"] for e in events]
    retry_scheduled_count = types.count(EventTypes.RETRY_SCHEDULED)
    retry_exhausted_count = types.count(EventTypes.RETRY_EXHAUSTED)
    assert retry_scheduled_count == 3
    assert retry_exhausted_count == 1


# --- Test 12: Query by run_id ---

def test_query_by_run_id(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "events.jsonl"
    sink = JsonlFileSink(jsonl_path)
    ctx = RuntimeContext(sinks=[sink])

    run1 = ctx.create_run()
    run2 = ctx.create_run()

    session1 = ctx.create_session(run1, agent_name="agent-1")
    session2 = ctx.create_session(run2, agent_name="agent-2")

    ctx.finish_session(session1)
    ctx.finish_session(session2)
    ctx.finish_run(run1)
    ctx.finish_run(run2)

    # Query for run1 only
    results = sink.query_by_run_id(run1.run_id)
    assert len(results) > 0
    assert all(r["run_id"] == run1.run_id for r in results)

    # Verify sorted by ts
    timestamps = [r["ts"] for r in results]
    assert timestamps == sorted(timestamps)


# --- Test 13: Invalid state transition ---

def test_invalid_state_transition(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    run = ctx.create_run()
    ctx.finish_run(run, RunStatus.SUCCEEDED)

    with pytest.raises(InvalidTransitionError):
        from veronica.runtime.state_machine import transition_run
        transition_run(run, RunStatus.RUNNING)


# --- Test 14: Events disabled via env var ---

def test_events_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERONICA_EVENTS", "0")
    jsonl_path = tmp_path / "events.jsonl"

    sinks = create_default_sinks(jsonl_path=jsonl_path)
    assert len(sinks) == 1
    assert isinstance(sinks[0], NullSink)

    ctx = RuntimeContext(sinks=sinks)
    run = ctx.create_run()
    ctx.finish_run(run)

    # No file should be created
    assert not jsonl_path.exists()


# --- Test 15: Result ref truncation ---

def test_result_ref_truncation() -> None:
    long_content = "x" * 1000
    ref = make_result_ref(long_content)

    parts = ref.split(":", 1)
    assert len(parts) == 2
    hash_part, preview_part = parts
    assert len(hash_part) == 16  # sha256 hex prefix
    assert len(preview_part) == 200  # truncated to 200
    assert preview_part == "x" * 200
