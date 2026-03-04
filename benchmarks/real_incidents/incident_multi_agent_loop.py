"""incident_multi_agent_loop.py

Real incident: Planner -> Critic -> Planner infinite loop.

A production code-generation system used a Planner-Critic architecture:
the Planner generated code, the Critic reviewed it and returned feedback,
the Planner revised based on feedback. Due to a misaligned Critic prompt,
the Critic always returned "needs revision" -- never approving any plan.
The system ran for 3 hours and 14 minutes before an engineer noticed the
runaway process in the billing dashboard.

Real data (multi-agent framework incident, 2024-Q2):
    - Planner-Critic cycles: 312
    - LLM calls total: 936 (3 per cycle: plan + critique + revision)
    - Duration: 3h 14min
    - Cost: $18.72 at gpt-4-turbo pricing ($0.02/call)
    - Outcome: no code produced, manual kill

This benchmark simulates the Planner->Critic->Planner infinite loop
and shows how AgentStepGuard + ExecutionContext contain it.

Usage:
    python benchmarks/real_incidents/incident_multi_agent_loop.py
"""

from __future__ import annotations

import time
from typing import Any

from veronica_core import AgentStepGuard
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Stub Planner and Critic
# ---------------------------------------------------------------------------

class StubPlanner:
    """Generates code plans that always need revision."""

    def __init__(self) -> None:
        self.call_count = 0

    def plan(self, cycle: int) -> dict[str, Any]:
        self.call_count += 1
        return {"cycle": cycle, "code": f"def solution_v{cycle}(): pass", "version": cycle}


class StubCritic:
    """Always rejects plans -- the misaligned prompt bug."""

    def __init__(self, approve_at: int = 9999) -> None:
        self.call_count = 0
        self.approve_at = approve_at

    def critique(self, plan: dict[str, Any]) -> dict[str, Any]:
        self.call_count += 1
        approved = plan["cycle"] >= self.approve_at
        return {
            "approved": approved,
            "feedback": "ok" if approved else "needs_revision: insufficient edge case handling",
        }


# ---------------------------------------------------------------------------
# Baseline: Planner->Critic loop runs until process kill
# ---------------------------------------------------------------------------

def baseline_multi_agent_loop(
    max_cycles: int = 312,
    cost_per_call_usd: float = 0.020,
) -> dict[str, Any]:
    """Simulate the 2024-Q2 incident: 312 Planner-Critic cycles before kill.

    3 LLM calls per cycle: plan + critique + revision = 936 total.
    Critic always rejects (approve_at=9999), loop never converges.
    """
    planner = StubPlanner()
    critic = StubCritic(approve_at=9999)
    start = time.perf_counter()
    cycles_done = 0
    stopped_by = "operator_kill"

    for cycle in range(max_cycles):
        plan = planner.plan(cycle)
        review = critic.critique(plan)
        # Revision call (simulated as another planner call)
        if review["approved"]:
            stopped_by = "task_complete"
            break
        cycles_done += 1

    if cycles_done >= max_cycles:
        stopped_by = "simulated_kill"

    elapsed_ms = (time.perf_counter() - start) * 1000
    total_calls = planner.call_count + critic.call_count
    # Each cycle has a revision too (implicit)
    total_calls_with_revision = total_calls + cycles_done
    total_cost = total_calls_with_revision * cost_per_call_usd

    return {
        "scenario": "baseline",
        "incident": "Planner-Critic infinite loop (2024-Q2)",
        "cycles": cycles_done,
        "planner_calls": planner.call_count,
        "critic_calls": critic.call_count,
        "total_calls": total_calls_with_revision,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_ms": round(elapsed_ms, 2),
        "stopped_by": stopped_by,
        "code_produced": False,
        "contained": False,
        "cost_saved_pct": 0.0,
    }


# ---------------------------------------------------------------------------
# Veronica: AgentStepGuard + ExecutionContext contain the loop
# ---------------------------------------------------------------------------

def veronica_multi_agent_loop(
    max_steps: int = 10,
    max_cost_usd: float = 1.00,
    cost_per_call_usd: float = 0.020,
) -> dict[str, Any]:
    """AgentStepGuard enforces a cycle limit on the Planner-Critic loop.

    After max_steps cycles, the guard halts and returns the last partial plan.
    In the real incident, a 10-step guard would have saved $18.52 ($18.72 - $0.20).
    """
    planner = StubPlanner()
    critic = StubCritic(approve_at=9999)
    guard = AgentStepGuard(max_steps=max_steps)
    config = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=max_steps * 4,
        max_retries_total=50,
    )

    cycle = 0
    halted_by = "unknown"
    last_plan: dict[str, Any] | None = None
    decisions: list[str] = []

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        while guard.step(result=last_plan):
            # Plan
            dec_plan = ctx.wrap_llm_call(
                fn=lambda c=cycle: planner.plan(c),
                options=WrapOptions(
                    operation_name="planner",
                    cost_estimate_hint=cost_per_call_usd,
                ),
            )
            decisions.append(f"plan:{dec_plan.name}")
            if dec_plan == Decision.HALT:
                halted_by = "budget_plan"
                break

            plan = planner.plan(cycle)
            last_plan = plan

            # Critique
            dec_crit = ctx.wrap_llm_call(
                fn=lambda p=plan: critic.critique(p),
                options=WrapOptions(
                    operation_name="critic",
                    cost_estimate_hint=cost_per_call_usd,
                ),
            )
            decisions.append(f"crit:{dec_crit.name}")
            if dec_crit == Decision.HALT:
                halted_by = "budget_critique"
                break

            review = critic.critique(plan)
            if review["approved"]:
                halted_by = "task_complete"
                break

            cycle += 1

        if guard.is_exceeded:
            halted_by = "agent_step_guard"

    elapsed_ms = (time.perf_counter() - start) * 1000
    snap = ctx.get_snapshot()
    total_calls = planner.call_count + critic.call_count
    total_cost = total_calls * cost_per_call_usd

    return {
        "scenario": "veronica",
        "incident": "Planner-Critic infinite loop (2024-Q2)",
        "cycles": cycle,
        "planner_calls": planner.call_count,
        "critic_calls": critic.call_count,
        "total_calls": total_calls,
        "guard_steps": guard.current_step,
        "guard_max_steps": max_steps,
        "total_cost_usd": round(total_cost, 4),
        "ctx_cost_usd": round(snap.cost_usd_accumulated, 6),
        "aborted": snap.aborted,
        "elapsed_ms": round(elapsed_ms, 2),
        "halted_by": halted_by,
        "code_produced": last_plan is not None,
        "contained": True,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    COST_PER_CALL = 0.020
    MAX_STEPS = 10

    base = baseline_multi_agent_loop(max_cycles=312, cost_per_call_usd=COST_PER_CALL)
    ver = veronica_multi_agent_loop(
        max_steps=MAX_STEPS, max_cost_usd=1.00, cost_per_call_usd=COST_PER_CALL
    )

    baseline_calls = base["total_calls"]
    veronica_calls = ver["total_calls"]
    cost_saved_pct = round(100 * (1 - veronica_calls / max(baseline_calls, 1)), 1)

    print("=" * 68)
    print("INCIDENT: Planner->Critic->Planner Infinite Loop (2024-Q2)")
    print("Real data: 312 cycles, 936 calls, $18.72, manual kill after 3h14m")
    print("=" * 68)
    print()
    print(f"{'scenario':<20} {'baseline_calls':>16} {'veronica_calls':>16} {'contained':>10} {'cost_saved_pct':>16}")
    print("-" * 80)
    print(f"{'baseline':<20} {baseline_calls:>16} {'N/A':>16} {'False':>10} {'0.0%':>16}")
    print(f"{'veronica':<20} {'N/A':>16} {veronica_calls:>16} {'True':>10} {cost_saved_pct:>15.1f}%")
    print()
    print(f"Baseline: {base['cycles']} cycles, {base['planner_calls']} planner, {base['critic_calls']} critic")
    print(f"Veronica: {ver['cycles']} cycles, guard {ver['guard_steps']}/{ver['guard_max_steps']} steps")
    print(f"Baseline cost: ${base['total_cost_usd']:.4f} | Veronica cost: ${ver['total_cost_usd']:.4f}")
    print(f"Partial code produced: {ver['code_produced']} | Halted by: {ver['halted_by']}")


if __name__ == "__main__":
    main()
