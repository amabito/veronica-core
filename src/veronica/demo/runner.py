"""VERONICA demo runner -- orchestrates all scenarios and writes JSONL output."""
from __future__ import annotations

import sys
from pathlib import Path

from veronica.runtime.events import event_to_dict
from veronica.runtime.sinks import JsonlFileSink

from veronica.demo import scenarios
from veronica.demo.render import render_scenario, render_summary

# ---------------------------------------------------------------------------
# BEFORE_TEXT strings -- one per scenario, printed before the timeline
# ---------------------------------------------------------------------------

_BEFORE_TEXT_RETRY_CASCADE = """\
Scenario: retry_cascade
Six LLM calls are attempted. The first five raise FakeProviderError(429).

Sub-systems exercised:
  - llm.call.failed recorded on each provider error
  - retry.scheduled / retry.exhausted after 3 retries
  - breaker.opened after retry exhaustion
  - control.degrade.level_changed as consecutive failures accumulate
  - Successful call 6 demonstrates recovery
"""

_BEFORE_TEXT_BUDGET_BURN = """\
Scenario: budget_burn
LLM calls are made in a loop at $0.01 each against a $0.10 limit.

Sub-systems exercised:
  - budget.reserve.ok on early calls (enforcer allows)
  - budget.commit after each successful call
  - budget.threshold_crossed at 80%, 90%, 100%
  - run.state_changed RUNNING -> DEGRADED -> HALTED
  - budget.reserve.denied when minute limit is exhausted
"""

_BEFORE_TEXT_TOOL_HANG = """\
Scenario: tool_hang
Three tool calls time out (FakeToolTimeout). The DegradeController escalates.

Sub-systems exercised:
  - tool.call.failed x3 (FakeToolTimeout)
  - control.degrade.level_changed NORMAL -> SOFT -> HARD
  - DegradedToolBlocked raised on call 4 (tools are now blocked)
  - Successful llm_call as graceful fallback on call 5
"""

_BEFORE_TEXT_RUNAWAY_AGENT = """\
Scenario: runaway_agent
Rapid concurrent calls exercise the Scheduler admission control paths.

Sub-systems exercised:
  - scheduler.admit.allowed (call 1 runs immediately)
  - scheduler.admit.queued / SchedulerQueued raised (call 2)
  - scheduler.admit.rejected / SchedulerRejected raised (call 3)
  - Inflight slot released; queued entry dispatched
  - Final call admitted and succeeds normally
"""

# Ordered scenario registry: (function, name, before_text)
_SCENARIOS: list[tuple[object, str, str]] = [
    (scenarios.retry_cascade, "retry_cascade", _BEFORE_TEXT_RETRY_CASCADE),
    (scenarios.budget_burn, "budget_burn", _BEFORE_TEXT_BUDGET_BURN),
    (scenarios.tool_hang, "tool_hang", _BEFORE_TEXT_TOOL_HANG),
    (scenarios.runaway_agent, "runaway_agent", _BEFORE_TEXT_RUNAWAY_AGENT),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_demo(jsonl_path: str = "./veronica-demo-events.jsonl") -> int:
    """Run all demo scenarios, print ASCII timelines, write events to JSONL.

    Args:
        jsonl_path: Destination path for the JSONL event log.

    Returns:
        Total number of events written across all scenarios.
    """
    sink = JsonlFileSink(path=jsonl_path)

    # Banner
    print()
    print("=" * 72)
    print("  VERONICA LLM Control OS")
    print("  CLI Demo -- Runtime Sub-system Walkthrough")
    print("=" * 72)
    print()

    total_events = 0

    for scenario_fn, name, before_text in _SCENARIOS:
        # Run scenario -- collect events via internal CollectorSink
        try:
            events = scenario_fn()  # type: ignore[operator]
        except Exception as exc:
            # Scenario raised an unexpected exception.  Print a warning and continue
            # so the demo completes all four scenarios regardless.
            print(f"  [WARN] scenario '{name}' raised {type(exc).__name__}: {exc}")
            print()
            continue

        # Write events to JSONL sink
        for event in events:
            sink.emit(event)
        total_events += len(events)

        # Render and print timeline
        rendered = render_scenario(name, before_text, events)
        print(rendered)

    # Final summary
    print(render_summary(total_events, jsonl_path))

    return total_events
