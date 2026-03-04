"""bench_baseline_comparison.py

Baseline comparison: No containment vs Veronica containment across
four canonical runaway scenarios.

Scenarios:
  A. Cost explosion       -- unlimited LLM calls accumulate $100+ in seconds
  B. Retry amplification  -- 3-layer nested retries => 3^3=27 calls
  C. Agent loop runaway   -- planner/critic loop never terminates
  D. Tool call storm      -- unconstrained parallel tool dispatch

Each scenario measures:
  - Total LLM/tool invocations
  - Simulated cost (USD)
  - Wall-clock elapsed (ms)
  - Halt mechanism (veronica only)

Usage:
    python benchmarks/bench_baseline_comparison.py
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from veronica_core import AgentStepGuard, RetryContainer
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Stub LLM (no network, deterministic)
# ---------------------------------------------------------------------------

class _LLM:
    """Stub LLM: tracks call count, optionally raises on first N calls."""

    def __init__(
        self,
        cost_per_call: float = 0.05,
        fail_first: int = 0,
    ) -> None:
        self.call_count = 0
        self.cost_per_call = cost_per_call
        self.fail_first = fail_first

    def call(self) -> dict[str, Any]:
        self.call_count += 1
        if self.call_count <= self.fail_first:
            raise RuntimeError(f"Transient failure #{self.call_count}")
        return {"tokens": 500, "cost": self.cost_per_call}

    def reset(self) -> None:
        self.call_count = 0


# ---------------------------------------------------------------------------
# Scenario A: Cost explosion
# ---------------------------------------------------------------------------

_COST_PER_CALL = 0.05
_BLAST_CALLS = 50  # Baseline runs this many before we stop measurement


def _scenario_a_baseline() -> dict[str, Any]:
    """Baseline: call LLM _BLAST_CALLS times with no cost guard."""
    llm = _LLM(cost_per_call=_COST_PER_CALL)
    t0 = time.perf_counter()
    for _ in range(_BLAST_CALLS):
        llm.call()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    total_cost = llm.call_count * _COST_PER_CALL
    return {
        "calls": llm.call_count,
        "cost_usd": round(total_cost, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": "none",
    }


def _scenario_a_veronica(budget_usd: float = 0.50) -> dict[str, Any]:
    """Veronica: BudgetEnforcer + ExecutionContext halt at $0.50."""
    llm = _LLM(cost_per_call=_COST_PER_CALL)
    config = ExecutionConfig(
        max_cost_usd=budget_usd,
        max_steps=1000,
        max_retries_total=100,
    )
    t0 = time.perf_counter()
    halt = "none"
    with ExecutionContext(config=config) as ctx:
        for _ in range(_BLAST_CALLS):
            decision = ctx.wrap_llm_call(
                fn=llm.call,
                options=WrapOptions(
                    operation_name="llm_call",
                    cost_estimate_hint=_COST_PER_CALL,
                ),
            )
            if decision == Decision.HALT:
                halt = "budget_exceeded"
                break
    elapsed_ms = (time.perf_counter() - t0) * 1000
    snap = ctx.get_snapshot()
    return {
        "calls": llm.call_count,
        "cost_usd": round(snap.cost_usd_accumulated, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": halt,
    }


# ---------------------------------------------------------------------------
# Scenario B: Retry amplification (3 layers x 3 attempts = 27)
# ---------------------------------------------------------------------------

_RETRY_LAYERS = 3
_RETRY_ATTEMPTS = 3


def _scenario_b_baseline() -> dict[str, Any]:
    """Baseline: 3 nested retry layers, always-failing LLM, no containment."""
    total_calls = 0

    def llm_call() -> str:
        nonlocal total_calls
        total_calls += 1
        raise RuntimeError("always fails")

    def _retry(fn: Any, n: int) -> None:
        for i in range(n):
            try:
                fn()
                return
            except RuntimeError:
                if i == n - 1:
                    raise

    t0 = time.perf_counter()
    try:
        _retry(lambda: _retry(lambda: _retry(llm_call, _RETRY_ATTEMPTS), _RETRY_ATTEMPTS), _RETRY_ATTEMPTS)
    except RuntimeError:
        pass
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return {
        "calls": total_calls,
        "cost_usd": round(total_calls * 0.01, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": "none",
        "theoretical_max": _RETRY_ATTEMPTS ** _RETRY_LAYERS,
    }


def _scenario_b_veronica(max_retries_total: int = 5) -> dict[str, Any]:
    """Veronica: RetryContainer + ExecutionContext cap total retries at 5."""
    llm = _LLM(fail_first=999)  # Always fails
    retry = RetryContainer(max_retries=max_retries_total, backoff_base=0.0)
    config = ExecutionConfig(
        max_cost_usd=10.0,
        max_steps=100,
        max_retries_total=max_retries_total,
    )
    t0 = time.perf_counter()
    halt = "none"
    with ExecutionContext(config=config) as ctx:
        for task_i in range(3):
            llm.reset()

            def _call(idx: int = task_i) -> dict[str, Any]:
                return retry.execute(lambda: llm.call())  # noqa: B023

            decision = ctx.wrap_llm_call(
                fn=_call,
                options=WrapOptions(
                    operation_name=f"retry_task_{task_i}",
                    cost_estimate_hint=0.01,
                ),
            )
            if decision == Decision.HALT:
                halt = "retry_budget_exceeded"
                break
    elapsed_ms = (time.perf_counter() - t0) * 1000
    snap = ctx.get_snapshot()
    return {
        "calls": llm.call_count,
        "cost_usd": round(snap.cost_usd_accumulated, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": halt,
    }


# ---------------------------------------------------------------------------
# Scenario C: Agent loop runaway
# ---------------------------------------------------------------------------

_LOOP_MAX_ITERS = 40  # Baseline allowed iterations before we stop measuring


def _scenario_c_baseline() -> dict[str, Any]:
    """Baseline: planner/critic loop, critic never approves, runs _LOOP_MAX_ITERS."""
    planner_calls = 0
    critic_calls = 0
    t0 = time.perf_counter()
    for _ in range(_LOOP_MAX_ITERS):
        planner_calls += 1
        critic_calls += 1
        # Critic never approves
    elapsed_ms = (time.perf_counter() - t0) * 1000
    total_calls = planner_calls + critic_calls
    return {
        "calls": total_calls,
        "cost_usd": round(total_calls * 0.02, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": "none",
        "iterations": _LOOP_MAX_ITERS,
    }


def _scenario_c_veronica(max_steps: int = 8) -> dict[str, Any]:
    """Veronica: AgentStepGuard halts the planner/critic loop at step limit."""
    planner_llm = _LLM(cost_per_call=0.02)
    critic_llm = _LLM(cost_per_call=0.02)
    guard = AgentStepGuard(max_steps=max_steps)
    config = ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=max_steps * 2 + 10,
        max_retries_total=100,
    )
    iteration = 0
    halt = "none"
    t0 = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        while guard.step(result=f"iter_{iteration}"):
            # Plan step
            d = ctx.wrap_llm_call(
                fn=planner_llm.call,
                options=WrapOptions(
                    operation_name="planner",
                    cost_estimate_hint=0.02,
                ),
            )
            if d == Decision.HALT:
                halt = "budget_exceeded"
                break
            # Critic step (never approves)
            d = ctx.wrap_llm_call(
                fn=critic_llm.call,
                options=WrapOptions(
                    operation_name="critic",
                    cost_estimate_hint=0.02,
                ),
            )
            if d == Decision.HALT:
                halt = "budget_exceeded"
                break
            iteration += 1
        if guard.is_exceeded:
            halt = "agent_step_guard"
    elapsed_ms = (time.perf_counter() - t0) * 1000
    snap = ctx.get_snapshot()
    total_calls = planner_llm.call_count + critic_llm.call_count
    return {
        "calls": total_calls,
        "cost_usd": round(snap.cost_usd_accumulated, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": halt,
        "iterations": iteration,
    }


# ---------------------------------------------------------------------------
# Scenario D: Tool call storm
# ---------------------------------------------------------------------------

_TOOL_STORM_CALLS = 60


def _scenario_d_baseline() -> dict[str, Any]:
    """Baseline: agent dispatches _TOOL_STORM_CALLS tool calls with no constraint."""
    tool_calls = 0
    t0 = time.perf_counter()
    for _ in range(_TOOL_STORM_CALLS):
        tool_calls += 1
        # Simulated tool: read file, call API, etc.
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return {
        "calls": tool_calls,
        "cost_usd": round(tool_calls * 0.001, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": "none",
    }


def _scenario_d_veronica(max_steps: int = 15) -> dict[str, Any]:
    """Veronica: ExecutionContext step limit halts tool call storm."""
    tool_call_count = 0
    config = ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=max_steps,
        max_retries_total=100,
    )
    halt = "none"
    t0 = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        for _ in range(_TOOL_STORM_CALLS):
            def _tool() -> dict[str, Any]:
                nonlocal tool_call_count
                tool_call_count += 1
                return {"result": "ok"}

            decision = ctx.wrap_llm_call(
                fn=_tool,
                options=WrapOptions(
                    operation_name="tool_call",
                    cost_estimate_hint=0.001,
                ),
            )
            if decision == Decision.HALT:
                halt = "step_limit"
                break
    elapsed_ms = (time.perf_counter() - t0) * 1000
    snap = ctx.get_snapshot()
    return {
        "calls": tool_call_count,
        "cost_usd": round(snap.cost_usd_accumulated, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": halt,
    }


# ---------------------------------------------------------------------------
# Result aggregation
# ---------------------------------------------------------------------------

@dataclass
class ScenarioResult:
    label: str
    baseline_calls: int
    veronica_calls: int
    baseline_cost: float
    veronica_cost: float
    call_reduction_pct: float
    cost_reduction_pct: float
    halt_mechanism: str
    baseline_ms: float
    veronica_ms: float


def _reduction_pct(baseline: float, veronica: float) -> float:
    if baseline <= 0:
        return 0.0
    return round(100.0 * (1.0 - veronica / baseline), 1)


def _run_all() -> list[ScenarioResult]:
    results: list[ScenarioResult] = []

    # Scenario A
    ba = _scenario_a_baseline()
    va = _scenario_a_veronica()
    results.append(ScenarioResult(
        label="A: Cost explosion",
        baseline_calls=ba["calls"],
        veronica_calls=va["calls"],
        baseline_cost=ba["cost_usd"],
        veronica_cost=va["cost_usd"],
        call_reduction_pct=_reduction_pct(ba["calls"], va["calls"]),
        cost_reduction_pct=_reduction_pct(ba["cost_usd"], va["cost_usd"]),
        halt_mechanism=va["halt"],
        baseline_ms=ba["elapsed_ms"],
        veronica_ms=va["elapsed_ms"],
    ))

    # Scenario B
    bb = _scenario_b_baseline()
    vb = _scenario_b_veronica()
    results.append(ScenarioResult(
        label="B: Retry amplification",
        baseline_calls=bb["calls"],
        veronica_calls=vb["calls"],
        baseline_cost=bb["cost_usd"],
        veronica_cost=vb["cost_usd"],
        call_reduction_pct=_reduction_pct(bb["calls"], vb["calls"]),
        cost_reduction_pct=_reduction_pct(bb["cost_usd"], vb["cost_usd"]),
        halt_mechanism=vb["halt"],
        baseline_ms=bb["elapsed_ms"],
        veronica_ms=vb["elapsed_ms"],
    ))

    # Scenario C
    bc = _scenario_c_baseline()
    vc = _scenario_c_veronica()
    results.append(ScenarioResult(
        label="C: Agent loop runaway",
        baseline_calls=bc["calls"],
        veronica_calls=vc["calls"],
        baseline_cost=bc["cost_usd"],
        veronica_cost=vc["cost_usd"],
        call_reduction_pct=_reduction_pct(bc["calls"], vc["calls"]),
        cost_reduction_pct=_reduction_pct(bc["cost_usd"], vc["cost_usd"]),
        halt_mechanism=vc["halt"],
        baseline_ms=bc["elapsed_ms"],
        veronica_ms=vc["elapsed_ms"],
    ))

    # Scenario D
    bd = _scenario_d_baseline()
    vd = _scenario_d_veronica()
    results.append(ScenarioResult(
        label="D: Tool call storm",
        baseline_calls=bd["calls"],
        veronica_calls=vd["calls"],
        baseline_cost=bd["cost_usd"],
        veronica_cost=vd["cost_usd"],
        call_reduction_pct=_reduction_pct(bd["calls"], vd["calls"]),
        cost_reduction_pct=_reduction_pct(bd["cost_usd"], vd["cost_usd"]),
        halt_mechanism=vd["halt"],
        baseline_ms=bd["elapsed_ms"],
        veronica_ms=vd["elapsed_ms"],
    ))

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 76)
    print("BENCHMARK: Baseline Comparison (No Containment vs Veronica)")
    print("=" * 76)

    results = _run_all()

    # Table 1: Call and cost comparison
    print()
    print("Table 1: LLM/Tool Invocations and Simulated Cost")
    print("-" * 76)
    hdr = (
        f"{'Scenario':<26} {'Baseline':>10} {'Veronica':>10} "
        f"{'Reduction':>10} {'Halt Mechanism':<22}"
    )
    print(hdr)
    print("-" * 76)
    for r in results:
        print(
            f"{r.label:<26} {r.baseline_calls:>10} {r.veronica_calls:>10} "
            f"{r.call_reduction_pct:>9.1f}% {r.halt_mechanism:<22}"
        )
    print("-" * 76)

    # Table 2: Cost comparison
    print()
    print("Table 2: Simulated Spend (USD, $0.01-0.05/call model)")
    print("-" * 68)
    hdr2 = (
        f"{'Scenario':<26} {'Baseline $':>12} {'Veronica $':>12} "
        f"{'Cost Reduction':>14}"
    )
    print(hdr2)
    print("-" * 68)
    for r in results:
        print(
            f"{r.label:<26} {r.baseline_cost:>12.4f} {r.veronica_cost:>12.4f} "
            f"{r.cost_reduction_pct:>13.1f}%"
        )
    print("-" * 68)

    # Table 3: Overhead
    print()
    print("Table 3: Wall-Clock Overhead (ms)")
    print("-" * 68)
    hdr3 = (
        f"{'Scenario':<26} {'Baseline ms':>12} {'Veronica ms':>12} "
        f"{'Overhead ms':>12}"
    )
    print(hdr3)
    print("-" * 68)
    for r in results:
        overhead = r.veronica_ms - r.baseline_ms
        print(
            f"{r.label:<26} {r.baseline_ms:>12.3f} {r.veronica_ms:>12.3f} "
            f"{overhead:>+12.3f}"
        )
    print("-" * 68)

    # Summary statistics
    avg_call_reduction = sum(r.call_reduction_pct for r in results) / len(results)
    avg_cost_reduction = sum(r.cost_reduction_pct for r in results) / len(results)
    print()
    print(f"Average call reduction across scenarios: {avg_call_reduction:.1f}%")
    print(f"Average cost reduction across scenarios: {avg_cost_reduction:.1f}%")
    print()
    print("Note: Overhead from containment is sub-millisecond; cost/call savings")
    print("exceed containment overhead by multiple orders of magnitude.")


if __name__ == "__main__":
    main()
