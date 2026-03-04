"""bench_multi_agent_loop.py

Measures multi-agent loop amplification: planner/critic/executor loop.
Compares uncontained (infinite loop risk) vs contained (AgentStepGuard + ExecutionContext).

Usage:
    python benchmarks/bench_multi_agent_loop.py
"""

from __future__ import annotations

import json
import time
from typing import Any

from veronica_core import AgentStepGuard
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Stub agents (no network)
# ---------------------------------------------------------------------------

class StubPlanner:
    """Generates a plan that always needs revision."""

    def __init__(self) -> None:
        self.call_count = 0

    def plan(self, iteration: int) -> dict[str, Any]:
        self.call_count += 1
        return {"iteration": iteration, "plan": f"plan_v{iteration}", "complete": False}


class StubCritic:
    """Always requests at least N revisions before approving."""

    def __init__(self, approve_after: int = 999) -> None:
        self.call_count = 0
        self.approve_after = approve_after

    def critique(self, plan: dict[str, Any]) -> dict[str, Any]:
        self.call_count += 1
        approved = plan["iteration"] >= self.approve_after
        return {"approved": approved, "feedback": "needs_revision" if not approved else "ok"}


class StubExecutor:
    """Executes the plan."""

    def __init__(self) -> None:
        self.call_count = 0

    def execute(self, plan: dict[str, Any]) -> dict[str, Any]:
        self.call_count += 1
        return {"executed": True, "plan": plan["plan"]}


# ---------------------------------------------------------------------------
# Baseline: planner/critic/executor loop without containment
# ---------------------------------------------------------------------------

def baseline_multi_agent_loop(max_iterations: int = 30) -> dict[str, Any]:
    """Simulated infinite loop — critic never approves, runs to max_iterations."""
    planner = StubPlanner()
    critic = StubCritic(approve_after=999)  # Never approves
    executor = StubExecutor()

    start = time.perf_counter()
    iteration = 0
    approved = False

    for iteration in range(max_iterations):
        plan = planner.plan(iteration)
        review = critic.critique(plan)
        if review["approved"]:
            executor.execute(plan)
            approved = True
            break
        # No containment: loop continues regardless

    elapsed_ms = (time.perf_counter() - start) * 1000
    total_calls = planner.call_count + critic.call_count + executor.call_count

    return {
        "scenario": "baseline",
        "total_calls": total_calls,
        "planner_calls": planner.call_count,
        "critic_calls": critic.call_count,
        "executor_calls": executor.call_count,
        "iterations": iteration + 1,
        "approved": approved,
        "stopped_by": "max_iterations_reached",
        "elapsed_ms": round(elapsed_ms, 2),
        "contained": False,
    }


# ---------------------------------------------------------------------------
# Veronica: AgentStepGuard + ExecutionContext contain the loop
# ---------------------------------------------------------------------------

def veronica_multi_agent_loop(
    max_steps: int = 10,
    max_cost_usd: float = 1.0,
    cost_per_call: float = 0.02,
) -> dict[str, Any]:
    """AgentStepGuard + ExecutionContext enforce step limit on the agent loop."""
    planner = StubPlanner()
    critic = StubCritic(approve_after=999)  # Never approves naturally
    executor = StubExecutor()

    guard = AgentStepGuard(max_steps=max_steps)
    config = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=max_steps * 3,  # 3 calls per iteration (plan/critique/execute)
        max_retries_total=100,
    )

    iteration = 0
    decisions: list[str] = []
    halted_by: str = "unknown"

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        # AgentStepGuard controls the outer loop
        while guard.step(result=f"iteration_{iteration}"):
            plan: dict[str, Any] | None = None
            review: dict[str, Any] | None = None

            # Step 1: Plan
            decision = ctx.wrap_llm_call(
                fn=lambda i=iteration: planner.plan(i),
                options=WrapOptions(
                    operation_name="planner",
                    cost_estimate_hint=cost_per_call,
                ),
            )
            decisions.append(f"plan:{decision.name}")
            if decision == Decision.HALT:
                halted_by = "execution_context_budget"
                break
            plan = planner.plan(iteration)  # Re-call for result (fn was already called)

            # Step 2: Critique
            decision = ctx.wrap_llm_call(
                fn=lambda p=plan: critic.critique(p),
                options=WrapOptions(
                    operation_name="critic",
                    cost_estimate_hint=cost_per_call,
                ),
            )
            decisions.append(f"critique:{decision.name}")
            if decision == Decision.HALT:
                halted_by = "execution_context_budget"
                break

            review = critic.critique(plan)
            if review["approved"]:
                ctx.wrap_llm_call(
                    fn=lambda p=plan: executor.execute(p),
                    options=WrapOptions(
                        operation_name="executor",
                        cost_estimate_hint=cost_per_call,
                    ),
                )
                halted_by = "task_complete"
                break

            iteration += 1

        if guard.is_exceeded:
            halted_by = "agent_step_guard"

    elapsed_ms = (time.perf_counter() - start) * 1000
    snap = ctx.get_snapshot()
    total_calls = planner.call_count + critic.call_count + executor.call_count

    return {
        "scenario": "veronica",
        "total_calls": total_calls,
        "planner_calls": planner.call_count,
        "critic_calls": critic.call_count,
        "executor_calls": executor.call_count,
        "iterations": iteration,
        "step_count": snap.step_count,
        "cost_usd": round(snap.cost_usd_accumulated, 6),
        "aborted": snap.aborted,
        "halted_by": halted_by,
        "guard_steps_used": guard.current_step,
        "guard_max_steps": max_steps,
        "decisions_sample": decisions[:6],
        "elapsed_ms": round(elapsed_ms, 2),
        "contained": True,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    MAX_ITERATIONS = 30
    MAX_STEPS = 8

    print("=" * 60)
    print("BENCHMARK: Multi-Agent Loop (planner/critic/executor)")
    print(f"Max iterations: {MAX_ITERATIONS} | Guard max_steps: {MAX_STEPS}")
    print("=" * 60)

    base = baseline_multi_agent_loop(max_iterations=MAX_ITERATIONS)
    ver = veronica_multi_agent_loop(
        max_steps=MAX_STEPS, max_cost_usd=10.0, cost_per_call=0.001
    )

    results = {
        "benchmark": "multi_agent_loop",
        "baseline": base,
        "veronica": ver,
        "call_reduction_pct": round(
            100 * (1 - ver["total_calls"] / max(base["total_calls"], 1)), 1
        ),
    }

    print(json.dumps(results, indent=2))

    print()
    print(
        f"{'Scenario':<20} {'Total Calls':>12} {'Iterations':>12} {'Halted By':>24}"
    )
    print("-" * 70)
    print(
        f"{'baseline':<20} {base['total_calls']:>12} {base['iterations']:>12} "
        f"{'max_iterations':>24}"
    )
    print(
        f"{'veronica':<20} {ver['total_calls']:>12} {ver['iterations']:>12} "
        f"{ver['halted_by']:>24}"
    )
    print(f"\nCall reduction: {results['call_reduction_pct']}%")
    print(f"Guard steps used: {ver['guard_steps_used']}/{ver['guard_max_steps']}")


if __name__ == "__main__":
    main()
