"""bench_recursive_tools.py

Measures recursive tool call depth: agent calls tool -> tool -> tool recursively.
Compares uncontained (unlimited depth) vs contained (max_steps limit).

Usage:
    python benchmarks/bench_recursive_tools.py
"""

from __future__ import annotations

import json
import time
from typing import Any

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Stub recursive tool
# ---------------------------------------------------------------------------

class RecursiveToolAgent:
    """Simulates an agent that calls a tool which triggers further tool calls."""

    def __init__(self) -> None:
        self.call_count = 0
        self.max_depth: int = 0

    def call_tool(self, depth: int = 0) -> dict[str, Any]:
        """Recursively calls itself, simulating tool -> tool -> tool chains."""
        self.call_count += 1
        self.max_depth = max(self.max_depth, depth)
        return {"depth": depth, "result": f"tool_result_{depth}"}

    def reset(self) -> None:
        self.call_count = 0
        self.max_depth = 0


# ---------------------------------------------------------------------------
# Baseline: unlimited recursive tool calls
# ---------------------------------------------------------------------------

def baseline_recursive_tools(target_depth: int = 20) -> dict[str, Any]:
    """No containment — runs to target_depth unconditionally."""
    agent = RecursiveToolAgent()

    start = time.perf_counter()
    for depth in range(target_depth):
        agent.call_tool(depth=depth)
    elapsed_ms = (time.perf_counter() - start) * 1000

    return {
        "scenario": "baseline",
        "total_calls": agent.call_count,
        "max_depth": agent.max_depth,
        "target_depth": target_depth,
        "stopped_by": "natural_end",
        "elapsed_ms": round(elapsed_ms, 2),
        "contained": False,
    }


# ---------------------------------------------------------------------------
# Veronica: step limit enforced by ExecutionContext
# ---------------------------------------------------------------------------

def veronica_recursive_tools(
    max_steps: int = 5,
    target_depth: int = 20,
    cost_per_call: float = 0.005,
) -> dict[str, Any]:
    """ExecutionContext enforces max_steps — stops recursive chain early."""
    agent = RecursiveToolAgent()

    config = ExecutionConfig(
        max_cost_usd=10.0,
        max_steps=max_steps,
        max_retries_total=100,
    )

    halted_at_depth: int | None = None
    decisions: list[str] = []

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        for depth in range(target_depth):
            captured_depth = depth

            decision = ctx.wrap_tool_call(
                fn=lambda d=captured_depth: agent.call_tool(depth=d),
                options=WrapOptions(
                    operation_name=f"tool_depth_{depth}",
                    cost_estimate_hint=cost_per_call,
                ),
            )
            decisions.append(decision.name)
            if decision == Decision.HALT:
                halted_at_depth = depth
                break

    elapsed_ms = (time.perf_counter() - start) * 1000
    snap = ctx.get_snapshot()

    return {
        "scenario": "veronica",
        "total_calls": agent.call_count,
        "max_depth_reached": agent.max_depth,
        "step_count": snap.step_count,
        "cost_usd": round(snap.cost_usd_accumulated, 6),
        "aborted": snap.aborted,
        "halted_at_depth": halted_at_depth,
        "decisions": decisions,
        "elapsed_ms": round(elapsed_ms, 2),
        "contained": True,
        "containment_depth": max_steps,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    TARGET_DEPTH = 20
    MAX_STEPS = 5

    print("=" * 60)
    print("BENCHMARK: Recursive Tool Calls")
    print(f"Target depth: {TARGET_DEPTH} | Step limit: {MAX_STEPS}")
    print("=" * 60)

    base = baseline_recursive_tools(target_depth=TARGET_DEPTH)
    ver = veronica_recursive_tools(
        max_steps=MAX_STEPS, target_depth=TARGET_DEPTH, cost_per_call=0.0001
    )

    results = {
        "benchmark": "recursive_tools",
        "baseline": base,
        "veronica": ver,
        "depth_reduction_pct": round(
            100 * (1 - ver["total_calls"] / max(base["total_calls"], 1)), 1
        ),
    }

    print(json.dumps(results, indent=2))

    print()
    print(
        f"{'Scenario':<20} {'Calls':>8} {'Depth':>8} {'Stopped By':>20} {'Elapsed ms':>12}"
    )
    print("-" * 70)
    print(
        f"{'baseline':<20} {base['total_calls']:>8} {base['max_depth']:>8} "
        f"{'natural_end':>20} {base['elapsed_ms']:>12.2f}"
    )
    print(
        f"{'veronica':<20} {ver['total_calls']:>8} {ver['max_depth_reached']:>8} "
        f"{'step_limit':>20} {ver['elapsed_ms']:>12.2f}"
    )
    print(f"\nDepth reduction: {results['depth_reduction_pct']}%")
    print(f"Halted at depth: {ver['halted_at_depth']}")


if __name__ == "__main__":
    main()
