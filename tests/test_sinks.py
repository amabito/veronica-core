"""Tests for event sinks: StdoutSink, JsonlFileSink, CompositeSink, ReporterBridgeSink."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from veronica.runtime.events import Event, EventTypes, make_event
from veronica.runtime.models import Severity
from veronica.runtime.sinks import (
    CompositeSink,
    JsonlFileSink,
    NullSink,
    ReporterBridgeSink,
    StdoutSink,
    create_default_sinks,
)


def _make_test_event(event_type: str = EventTypes.RUN_CREATED, run_id: str = "r1") -> Event:
    return make_event(event_type, run_id, payload={"test": True})


# --- NullSink ---


def test_null_sink_discards():
    sink = NullSink()
    sink.emit(_make_test_event())  # no error


# --- StdoutSink ---


def test_stdout_sink_prints(capsys):
    sink = StdoutSink()
    sink.emit(_make_test_event())
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["type"] == EventTypes.RUN_CREATED
    assert data["run_id"] == "r1"


def test_stdout_sink_severity_filter(capsys):
    sink = StdoutSink(min_severity="warn")
    # info event should be filtered
    sink.emit(_make_test_event())
    captured = capsys.readouterr()
    assert captured.out == ""

    # warn event should pass
    evt = make_event(EventTypes.BUDGET_EXCEEDED, "r1", severity=Severity.WARN)
    sink.emit(evt)
    captured = capsys.readouterr()
    assert EventTypes.BUDGET_EXCEEDED in captured.out


# --- JsonlFileSink ---


def test_jsonl_file_sink_write_and_query(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlFileSink(path)

    evt1 = make_event(EventTypes.RUN_CREATED, "run-a")
    evt2 = make_event(EventTypes.STEP_STARTED, "run-a")
    evt3 = make_event(EventTypes.RUN_CREATED, "run-b")

    sink.emit(evt1)
    sink.emit(evt2)
    sink.emit(evt3)

    results = sink.query_by_run_id("run-a")
    assert len(results) == 2
    assert all(r["run_id"] == "run-a" for r in results)


def test_jsonl_file_sink_query_empty(tmp_path):
    path = tmp_path / "empty.jsonl"
    sink = JsonlFileSink(path)
    assert sink.query_by_run_id("nonexistent") == []


def test_jsonl_file_sink_close(tmp_path):
    sink = JsonlFileSink(tmp_path / "events.jsonl")
    sink.close()  # no-op, should not raise


# --- CompositeSink ---


def test_composite_sink_fans_out():
    collector1: list[Event] = []
    collector2: list[Event] = []

    class ListSink:
        def __init__(self, lst):
            self._lst = lst

        def emit(self, event):
            self._lst.append(event)

    composite = CompositeSink([ListSink(collector1), ListSink(collector2)])
    evt = _make_test_event()
    composite.emit(evt)

    assert len(collector1) == 1
    assert len(collector2) == 1


def test_composite_sink_error_isolation():
    class FailingSink:
        def emit(self, event):
            raise RuntimeError("boom")

    collector: list[Event] = []

    class GoodSink:
        def emit(self, event):
            collector.append(event)

    composite = CompositeSink([FailingSink(), GoodSink()])
    composite.emit(_make_test_event())  # should not raise
    assert len(collector) == 1


def test_composite_sink_query_delegates(tmp_path):
    path = tmp_path / "events.jsonl"
    jsonl = JsonlFileSink(path)
    jsonl.emit(make_event(EventTypes.RUN_CREATED, "run-x"))

    composite = CompositeSink([NullSink(), jsonl])
    results = composite.query_by_run_id("run-x")
    assert len(results) == 1


def test_composite_sink_query_no_queryable():
    composite = CompositeSink([NullSink()])
    assert composite.query_by_run_id("anything") == []


# --- ReporterBridgeSink ---


def test_reporter_bridge_maps_event():
    reporter = Mock()
    sink = ReporterBridgeSink(reporter)

    evt = make_event(
        EventTypes.RUN_CREATED, "run-1",
        session_id="s1", step_id="st1",
        payload={"budget": 5.0},
    )
    sink.emit(evt)

    reporter.send.assert_called_once()
    args = reporter.send.call_args[0]
    assert args[0] == "chain_start"
    assert args[1] == "run-1"
    assert args[2]["original_type"] == EventTypes.RUN_CREATED
    assert args[2]["session_id"] == "s1"
    assert args[2]["step_id"] == "st1"


def test_reporter_bridge_skips_unmapped():
    reporter = Mock()
    sink = ReporterBridgeSink(reporter)

    evt = make_event(EventTypes.SESSION_CREATED, "run-1")
    sink.emit(evt)

    reporter.send.assert_not_called()


def test_reporter_bridge_handles_send_error():
    reporter = Mock()
    reporter.send.side_effect = RuntimeError("network error")
    sink = ReporterBridgeSink(reporter)

    evt = make_event(EventTypes.RUN_CREATED, "run-1")
    sink.emit(evt)  # should not raise


# --- create_default_sinks ---


def test_create_default_sinks_normal(monkeypatch):
    monkeypatch.delenv("VERONICA_EVENTS", raising=False)
    sinks = create_default_sinks()
    assert len(sinks) == 2
    assert isinstance(sinks[0], StdoutSink)
    assert isinstance(sinks[1], JsonlFileSink)


def test_create_default_sinks_disabled(monkeypatch):
    monkeypatch.setenv("VERONICA_EVENTS", "0")
    sinks = create_default_sinks()
    assert len(sinks) == 1
    assert isinstance(sinks[0], NullSink)
