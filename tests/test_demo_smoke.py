"""Smoke tests for the VERONICA CLI demo -- four scenarios and the runner."""
from __future__ import annotations

from pathlib import Path

from veronica.demo import scenarios
from veronica.demo.runner import run_demo


def test_demo_retry_cascade_produces_events() -> None:
    """retry_cascade emits events for retries and circuit breaker."""
    events = scenarios.retry_cascade()
    types = [e.type for e in events]

    assert len(events) > 0
    assert "breaker.opened" in types
    assert "retry.scheduled" in types


def test_demo_budget_burn_produces_events() -> None:
    """budget_burn emits threshold-crossed and reserve-denied events."""
    events = scenarios.budget_burn()
    types = [e.type for e in events]

    assert len(events) > 0
    assert "budget.threshold_crossed" in types
    assert "budget.reserve.denied" in types


def test_demo_tool_hang_produces_events() -> None:
    """tool_hang emits degrade level change events when tools time out."""
    events = scenarios.tool_hang()
    types = [e.type for e in events]

    assert len(events) > 0
    assert "control.degrade.level_changed" in types


def test_demo_runaway_agent_produces_events() -> None:
    """runaway_agent emits scheduler rejection events under saturation."""
    events = scenarios.runaway_agent()
    types = [e.type for e in events]

    assert len(events) > 0
    assert "scheduler.admit.rejected" in types


def test_demo_runner_writes_jsonl(tmp_path: Path) -> None:
    """run_demo writes all scenario events to the JSONL output file."""
    path = tmp_path / "test-demo-events.jsonl"
    run_demo(str(path))

    assert path.exists(), "JSONL output file was not created"
    lines = [l for l in path.read_text().strip().split("\n") if l.strip()]
    assert len(lines) > 20, f"Expected >20 JSONL lines, got {len(lines)}"


def test_demo_all_scenarios_deterministic() -> None:
    """Each scenario produces the same sequence of event types on repeated runs."""
    scenario_fns = [
        scenarios.retry_cascade,
        scenarios.budget_burn,
        scenarios.tool_hang,
        scenarios.runaway_agent,
    ]
    for fn in scenario_fns:
        run1 = [e.type for e in fn()]
        run2 = [e.type for e in fn()]
        assert run1 == run2, (
            f"{fn.__name__} is not deterministic: "
            f"run1 has {len(run1)} events, run2 has {len(run2)} events"
        )
