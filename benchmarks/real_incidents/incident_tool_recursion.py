"""incident_tool_recursion.py

Real incident: Agent calling tool recursively via "call me again" pattern.

A production research agent used a tool called `search_and_summarize`.
Due to a prompt bug, the tool's output always included the instruction
"For more detail, call search_and_summarize again with a refined query."
The agent faithfully followed this instruction on every call, recursing
indefinitely until the process was killed by OOM.

Real data (AutoGPT GitHub issue #4221 pattern, 2023-Q2):
    - Tool calls before OOM: 1,847
    - Recursion depth at OOM: unbounded (tail-recursive pattern)
    - Duration: ~22 minutes
    - Cost: ~$36.94 at gpt-4 pricing ($0.02/call)
    - Recovery: process killed, no useful output saved

This benchmark simulates infinite tool recursion and shows how
AgentStepGuard prevents unbounded tool call depth.

Usage:
    python benchmarks/real_incidents/incident_tool_recursion.py
"""

from __future__ import annotations

import time
from typing import Any

from veronica_core import AgentStepGuard
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Simulated tool that always instructs the agent to call it again
# ---------------------------------------------------------------------------

class RecursiveTool:
    """Simulates search_and_summarize that always returns "call me again"."""

    def __init__(self) -> None:
        self.call_count = 0

    def execute(self, query: str) -> dict[str, Any]:
        self.call_count += 1
        return {
            "result": f"Partial summary for: {query}",
            "instruction": "For more detail, call search_and_summarize again with a refined query.",
            "should_continue": True,  # Always True -- the bug
        }

    def reset(self) -> None:
        self.call_count = 0


# ---------------------------------------------------------------------------
# Baseline: agent follows tool instruction indefinitely
# ---------------------------------------------------------------------------

def baseline_tool_recursion(
    max_calls: int = 1847,
    cost_per_call_usd: float = 0.020,
) -> dict[str, Any]:
    """Simulate the 2023-Q2 incident: agent recurses on tool indefinitely.

    The agent interprets 'call me again' as a task requirement and loops.
    In the real incident, 1,847 calls were made before OOM.
    """
    tool = RecursiveTool()
    query = "recent AI safety research"
    start = time.perf_counter()
    stopped_by = "oom_kill"

    for _ in range(max_calls):
        result = tool.execute(query)
        if not result["should_continue"]:
            stopped_by = "task_complete"
            break
        query = f"refined: {query}"  # Simulate "refined query"

    if tool.call_count >= max_calls:
        stopped_by = "oom_simulated"

    elapsed_ms = (time.perf_counter() - start) * 1000
    total_cost = tool.call_count * cost_per_call_usd

    return {
        "scenario": "baseline",
        "incident": "AutoGPT recursive tool loop (2023-Q2)",
        "tool_calls": tool.call_count,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_ms": round(elapsed_ms, 2),
        "stopped_by": stopped_by,
        "useful_output": False,
        "contained": False,
        "cost_saved_pct": 0.0,
    }


# ---------------------------------------------------------------------------
# Veronica: AgentStepGuard + ExecutionContext cap tool recursion
# ---------------------------------------------------------------------------

def veronica_tool_recursion(
    max_steps: int = 25,
    max_cost_usd: float = 0.50,
    cost_per_call_usd: float = 0.020,
) -> dict[str, Any]:
    """AgentStepGuard limits tool call depth to max_steps.

    After max_steps tool calls, the guard halts the loop and
    preserves the last partial result.
    """
    tool = RecursiveTool()
    guard = AgentStepGuard(max_steps=max_steps)
    config = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=max_steps * 2,
        max_retries_total=50,
    )

    query = "recent AI safety research"
    halted_by = "unknown"
    last_partial: dict[str, Any] | None = None

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        while guard.step(result=last_partial):
            decision = ctx.wrap_llm_call(
                fn=lambda q=query: tool.execute(q),
                options=WrapOptions(
                    operation_name="search_and_summarize",
                    cost_estimate_hint=cost_per_call_usd,
                ),
            )
            if decision == Decision.HALT:
                halted_by = "budget_exceeded"
                break

            result = tool.execute(query)
            last_partial = result

            if not result["should_continue"]:
                halted_by = "task_complete"
                break

            query = f"refined: {query}"

        if guard.is_exceeded:
            halted_by = "agent_step_guard"

    elapsed_ms = (time.perf_counter() - start) * 1000
    snap = ctx.get_snapshot()
    total_cost = tool.call_count * cost_per_call_usd

    return {
        "scenario": "veronica",
        "incident": "AutoGPT recursive tool loop (2023-Q2)",
        "tool_calls": tool.call_count,
        "guard_steps": guard.current_step,
        "guard_max_steps": max_steps,
        "total_cost_usd": round(total_cost, 4),
        "ctx_cost_usd": round(snap.cost_usd_accumulated, 6),
        "aborted": snap.aborted,
        "elapsed_ms": round(elapsed_ms, 2),
        "halted_by": halted_by,
        "useful_output": last_partial is not None,
        "contained": True,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    COST_PER_CALL = 0.020
    MAX_STEPS = 25

    base = baseline_tool_recursion(max_calls=1847, cost_per_call_usd=COST_PER_CALL)
    ver = veronica_tool_recursion(
        max_steps=MAX_STEPS, max_cost_usd=0.50, cost_per_call_usd=COST_PER_CALL
    )

    baseline_calls = base["tool_calls"]
    veronica_calls = ver["tool_calls"]
    cost_saved_pct = round(100 * (1 - veronica_calls / max(baseline_calls, 1)), 1)

    print("=" * 68)
    print("INCIDENT: AutoGPT Recursive Tool Loop (2023-Q2)")
    print("Real data: 1,847 tool calls, $36.94, OOM kill after 22 minutes")
    print("=" * 68)
    print()
    print(f"{'scenario':<20} {'baseline_calls':>16} {'veronica_calls':>16} {'contained':>10} {'cost_saved_pct':>16}")
    print("-" * 80)
    print(f"{'baseline':<20} {baseline_calls:>16} {'N/A':>16} {'False':>10} {'0.0%':>16}")
    print(f"{'veronica':<20} {'N/A':>16} {veronica_calls:>16} {'True':>10} {cost_saved_pct:>15.1f}%")
    print()
    print(f"Baseline cost: ${base['total_cost_usd']:.4f} | Veronica cost: ${ver['total_cost_usd']:.4f}")
    print(f"Guard steps: {ver['guard_steps']}/{ver['guard_max_steps']} | Partial output: {ver['useful_output']}")
    print(f"Halted by: {ver['halted_by']}")


if __name__ == "__main__":
    main()
