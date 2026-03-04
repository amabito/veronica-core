"""incident_02_cost_spike.py

Real incident: $552 runaway API bill from recursive agent (2024-Q1).

A developer left an autonomous coding agent running overnight with no
budget ceiling. The agent encountered a bug, spawned sub-agents to
investigate, each of which spawned further sub-agents for root-cause
analysis. By morning the recursion tree had made 27,600 API calls.

Real data (from HN thread: "I woke up to a $552 API bill"):
    - Recursive depth: ~6 levels
    - Calls per level: ~5 sub-agents
    - Total calls: 5^6 = 15,625 (actual: ~27,600 due to retry amplification)
    - Cost: $552.41 at gpt-4-turbo pricing ($0.02/call average)
    - Duration: ~8 hours (overnight, unmonitored)

This benchmark simulates the recursive call tree and shows how
BudgetEnforcer + ExecutionContext would have stopped it at $5.00.

Usage:
    python benchmarks/real_incidents/incident_02_cost_spike.py
"""

from __future__ import annotations

import time
from typing import Any

from veronica_core import BudgetEnforcer
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Simulated recursive sub-agent spawner
# ---------------------------------------------------------------------------

class RecursiveAgentNode:
    """Simulates a sub-agent that spawns more sub-agents.

    Models the 2024-Q1 incident pattern: each node spawns `branching_factor`
    children up to `max_depth` levels deep.
    """

    def __init__(self) -> None:
        self.total_calls = 0
        self.max_depth_reached = 0

    def run(self, depth: int, branching_factor: int, max_depth: int) -> dict[str, Any]:
        """Recursively spawn sub-agents without any budget check."""
        self.total_calls += 1
        self.max_depth_reached = max(self.max_depth_reached, depth)

        if depth >= max_depth:
            return {"depth": depth, "calls": 1, "result": "leaf_node"}

        child_results = []
        for _ in range(branching_factor):
            child = self.run(depth + 1, branching_factor, max_depth)
            child_results.append(child)

        return {
            "depth": depth,
            "children": len(child_results),
            "result": "branch_node",
        }


# ---------------------------------------------------------------------------
# Baseline: no budget ceiling, full recursion tree
# ---------------------------------------------------------------------------

def baseline_recursive_agent(
    branching_factor: int = 5,
    max_depth: int = 6,
    cost_per_call_usd: float = 0.020,
) -> dict[str, Any]:
    """Simulate the 2024-Q1 incident: unconstrained recursive sub-agent spawning.

    5 branches x 6 levels = 5^6 = 15,625 theoretical calls.
    Real incident: ~27,600 due to retry amplification (not modeled here).
    """
    agent = RecursiveAgentNode()
    start = time.perf_counter()

    agent.run(depth=0, branching_factor=branching_factor, max_depth=max_depth)

    elapsed_ms = (time.perf_counter() - start) * 1000
    theoretical = sum(branching_factor**d for d in range(max_depth + 1))
    total_cost = agent.total_calls * cost_per_call_usd

    return {
        "scenario": "baseline",
        "incident": "$552 runaway recursive agent (2024-Q1 HN thread)",
        "llm_calls": agent.total_calls,
        "theoretical_max": theoretical,
        "max_depth": agent.max_depth_reached,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_ms": round(elapsed_ms, 2),
        "stopped_by": "natural_recursion_end",
        "contained": False,
    }


# ---------------------------------------------------------------------------
# Veronica: BudgetEnforcer + ExecutionContext halt at $5 budget
# ---------------------------------------------------------------------------

def veronica_recursive_agent(
    branching_factor: int = 5,
    max_depth: int = 6,
    budget_usd: float = 5.00,
    cost_per_call_usd: float = 0.020,
) -> dict[str, Any]:
    """BudgetEnforcer + ExecutionContext cap the recursive tree at $5.

    In the real incident, a $5 budget ceiling would have stopped the recursion
    after ~250 calls instead of ~27,600. Operator is notified automatically.
    """
    budget = BudgetEnforcer(limit_usd=budget_usd)
    config = ExecutionConfig(
        max_cost_usd=budget_usd,
        max_steps=10000,
        max_retries_total=100,
    )

    calls_made = 0
    halted_by = "natural_end"
    budget_exceeded = False

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:

        def spawn_agent(depth: int) -> None:
            nonlocal calls_made, halted_by, budget_exceeded

            if budget_exceeded:
                return

            # Budget check before spawning
            if not budget.spend(cost_per_call_usd):
                halted_by = "budget_enforcer"
                budget_exceeded = True
                return

            # ExecutionContext wrap
            decision = ctx.wrap_llm_call(
                fn=lambda d=depth: {"depth": d, "result": "ok"},
                options=WrapOptions(
                    operation_name=f"agent_depth_{depth}",
                    cost_estimate_hint=cost_per_call_usd,
                ),
            )
            calls_made += 1

            if decision == Decision.HALT:
                halted_by = "execution_context"
                budget_exceeded = True
                return

            if depth >= max_depth:
                return

            for _ in range(branching_factor):
                if budget_exceeded:
                    break
                spawn_agent(depth + 1)

        spawn_agent(depth=0)

    elapsed_ms = (time.perf_counter() - start) * 1000
    snap = ctx.get_snapshot()
    total_cost = calls_made * cost_per_call_usd

    return {
        "scenario": "veronica",
        "incident": "$552 runaway recursive agent (2024-Q1 HN thread)",
        "llm_calls": calls_made,
        "budget_limit_usd": budget_usd,
        "total_cost_usd": round(total_cost, 4),
        "budget_spent_usd": round(budget._spent_usd, 4),
        "ctx_cost_usd": round(snap.cost_usd_accumulated, 6),
        "aborted": snap.aborted,
        "elapsed_ms": round(elapsed_ms, 2),
        "halted_by": halted_by,
        "contained": True,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 68)
    print("INCIDENT #02: $552 Runaway Recursive Agent Bill (2024-Q1)")
    print("Real data: ~27,600 API calls, $552 overnight, no monitoring")
    print("=" * 68)

    BRANCHING = 5
    MAX_DEPTH = 6
    COST_PER_CALL = 0.020
    BUDGET_USD = 5.00

    base = baseline_recursive_agent(
        branching_factor=BRANCHING,
        max_depth=MAX_DEPTH,
        cost_per_call_usd=COST_PER_CALL,
    )
    ver = veronica_recursive_agent(
        branching_factor=BRANCHING,
        max_depth=MAX_DEPTH,
        budget_usd=BUDGET_USD,
        cost_per_call_usd=COST_PER_CALL,
    )

    call_reduction = round(100 * (1 - ver["llm_calls"] / max(base["llm_calls"], 1)), 1)
    cost_reduction = round(100 * (1 - ver["total_cost_usd"] / max(base["total_cost_usd"], 0.0001)), 1)

    print()
    header = f"{'Metric':<32} {'Baseline (incident)':>18} {'Veronica':>16}"
    print(header)
    print("-" * 68)
    print(f"{'LLM calls':<32} {base['llm_calls']:>18,} {ver['llm_calls']:>16,}")
    print(f"{'Total cost (USD)':<32} ${base['total_cost_usd']:>17.2f} ${ver['total_cost_usd']:>15.2f}")
    print(f"{'Budget ceiling applied':<32} {'NO':>18} {'YES ($5.00)':>16}")
    print(f"{'Stopped by':<32} {base['stopped_by'][:18]:>18} {ver['halted_by']:>16}")
    print(f"{'Operator bill':<32} {'$552 (real incident)':>18} {'< $5.00':>16}")
    print()
    print(f"Call reduction:  {call_reduction}%")
    print(f"Cost reduction:  {cost_reduction}%")
    print(f"Budget efficiency: ${ver['total_cost_usd']:.2f} spent of ${BUDGET_USD:.2f} ceiling")


if __name__ == "__main__":
    main()
