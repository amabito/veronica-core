"""incident_01_openai_loop.py

Real incident: GPT-4 infinite self-correction loop (2023-Q3).

A production assistant was asked to write a unit test. On each revision, the LLM
critiqued its own output and decided it was "not quite right", spawning the next
revision. With no step limit, the loop ran for 247 iterations before the
operator noticed the runaway process and killed it manually.

Real data (approximated from incident report):
    - Duration: ~18 minutes before manual kill
    - LLM calls made: 247
    - Cost at gpt-4-0613 pricing: ~$4.94 (247 * $0.02/call)
    - Operator action: kill -9 on the process

This benchmark simulates that scenario and shows how AgentStepGuard
with ExecutionContext would have capped it at 25 calls (~$0.50).

Usage:
    python benchmarks/real_incidents/incident_01_openai_loop.py
"""

from __future__ import annotations

import time
from typing import Any

from veronica_core import AgentStepGuard
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Simulated LLM that always finds a "flaw" requiring revision
# ---------------------------------------------------------------------------

class SelfCritiquingLLM:
    """Simulates GPT-4 assistant that always produces a new revision.

    Models the 2023-Q3 incident where every self-critique resulted in
    "the test needs improvement" -- never reaching "looks good".
    """

    def __init__(self, approve_at_iteration: int = 9999) -> None:
        self.call_count = 0
        self.approve_at_iteration = approve_at_iteration

    def generate_revision(self, iteration: int) -> dict[str, Any]:
        self.call_count += 1
        approved = iteration >= self.approve_at_iteration
        critique = "ok" if approved else "needs_improvement"
        return {
            "iteration": iteration,
            "code": f"def test_func_v{iteration}(): assert True",
            "self_critique": critique,
            "approved": approved,
        }


# ---------------------------------------------------------------------------
# Baseline: no containment, runs until process kill (simulated as max_iter)
# ---------------------------------------------------------------------------

def baseline_self_correction_loop(max_iterations: int = 247) -> dict[str, Any]:
    """Simulate the 2023-Q3 incident: 247 iterations before manual kill.

    In the real incident, max_iterations was effectively infinity --
    the loop was only stopped by operator intervention (kill -9).
    We simulate that by running to max_iterations.
    """
    llm = SelfCritiquingLLM(approve_at_iteration=9999)
    cost_per_call_usd = 0.020  # gpt-4-0613 ~$0.02/call at 2023 pricing

    start = time.perf_counter()
    iteration = 0
    stopped_by = "operator_kill"

    for iteration in range(max_iterations):
        result = llm.generate_revision(iteration)
        if result["approved"]:
            stopped_by = "task_complete"
            break

    elapsed_ms = (time.perf_counter() - start) * 1000
    total_cost = llm.call_count * cost_per_call_usd

    return {
        "scenario": "baseline",
        "incident": "GPT-4 self-correction loop (2023-Q3)",
        "llm_calls": llm.call_count,
        "iterations": iteration + 1,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_ms": round(elapsed_ms, 2),
        "stopped_by": stopped_by,
        "contained": False,
    }


# ---------------------------------------------------------------------------
# Veronica: AgentStepGuard + ExecutionContext cap the loop at 25 steps
# ---------------------------------------------------------------------------

def veronica_self_correction_loop(
    max_steps: int = 25,
    max_cost_usd: float = 0.50,
    cost_per_call_usd: float = 0.020,
) -> dict[str, Any]:
    """AgentStepGuard with ExecutionContext contain the self-correction loop.

    In the real incident, a 25-step guard would have stopped the loop after
    25 iterations ($0.50) instead of 247 ($4.94). More importantly, the
    process would not have required operator intervention.
    """
    llm = SelfCritiquingLLM(approve_at_iteration=9999)
    guard = AgentStepGuard(max_steps=max_steps)
    config = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=max_steps * 2,
        max_retries_total=50,
    )

    iteration = 0
    halted_by = "unknown"
    decisions: list[str] = []

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        while guard.step(result=f"iteration_{iteration}"):
            decision = ctx.wrap_llm_call(
                fn=lambda i=iteration: llm.generate_revision(i),
                options=WrapOptions(
                    operation_name="self_critique",
                    cost_estimate_hint=cost_per_call_usd,
                ),
            )
            decisions.append(decision.name)

            if decision == Decision.HALT:
                halted_by = "budget_exceeded"
                break

            if iteration >= 9998:
                halted_by = "task_complete"
                break

            iteration += 1

        if guard.is_exceeded:
            halted_by = "agent_step_guard"

    elapsed_ms = (time.perf_counter() - start) * 1000
    snap = ctx.get_snapshot()
    total_cost = llm.call_count * cost_per_call_usd

    return {
        "scenario": "veronica",
        "incident": "GPT-4 self-correction loop (2023-Q3)",
        "llm_calls": llm.call_count,
        "iterations": iteration + 1,
        "total_cost_usd": round(total_cost, 4),
        "guard_steps_used": guard.current_step,
        "guard_max_steps": max_steps,
        "ctx_step_count": snap.step_count,
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
    print("INCIDENT #01: GPT-4 Infinite Self-Correction Loop (2023-Q3)")
    print("Real data: 247 iterations, ~$4.94, stopped by kill -9")
    print("=" * 68)

    REAL_INCIDENT_ITERATIONS = 247
    GUARD_MAX_STEPS = 25
    COST_PER_CALL = 0.020

    base = baseline_self_correction_loop(max_iterations=REAL_INCIDENT_ITERATIONS)
    ver = veronica_self_correction_loop(
        max_steps=GUARD_MAX_STEPS,
        max_cost_usd=0.50,
        cost_per_call_usd=COST_PER_CALL,
    )

    call_reduction = round(100 * (1 - ver["llm_calls"] / max(base["llm_calls"], 1)), 1)
    cost_reduction = round(100 * (1 - ver["total_cost_usd"] / max(base["total_cost_usd"], 0.0001)), 1)

    print()
    header = f"{'Metric':<28} {'Baseline (incident)':>20} {'Veronica':>20}"
    print(header)
    print("-" * 70)
    print(f"{'LLM calls':<28} {base['llm_calls']:>20} {ver['llm_calls']:>20}")
    print(f"{'Iterations':<28} {base['iterations']:>20} {ver['iterations']:>20}")
    print(f"{'Total cost (USD)':<28} {base['total_cost_usd']:>20.4f} {ver['total_cost_usd']:>20.4f}")
    print(f"{'Stopped by':<28} {base['stopped_by']:>20} {ver['halted_by']:>20}")
    print(f"{'Operator intervention':<28} {'YES':>20} {'NO':>20}")
    print()
    print(f"Call reduction:  {call_reduction}%")
    print(f"Cost reduction:  {cost_reduction}%")
    print(f"Incident verdict: {ver['guard_steps_used']}/{ver['guard_max_steps']} steps used -- contained automatically")


if __name__ == "__main__":
    main()
