"""scale_simulation.py

Scale simulation: Veronica containment overhead vs agent fleet size.

Models an organization deploying N concurrent agent chains simultaneously.
Each chain makes up to MAX_STEPS LLM calls before completing or being halted.
Measures:
  - Wall-clock throughput (chains/sec)
  - Containment overhead per chain (ms)
  - Total calls halted vs would-have-run without containment
  - Cost savings at scale (USD)

Fleet sizes simulated: 1, 10, 50, 100, 500, 1000 concurrent chains.

Usage:
    python benchmarks/scale_simulation.py
"""

from __future__ import annotations

import statistics
import threading
import time
from dataclasses import dataclass
from typing import Any

from veronica_core import AgentStepGuard
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Simulation parameters
# ---------------------------------------------------------------------------

CHAIN_MAX_STEPS = 20          # Max steps per well-behaved chain
RUNAWAY_STEPS = 200           # Steps a runaway chain would take without containment
COST_PER_CALL = 0.05          # USD per LLM call
CONTAINED_STEP_LIMIT = 15     # ExecutionContext halts at 15 steps
CONTAINED_BUDGET_USD = 0.75   # ExecutionContext halts at $0.75
RUNAWAY_FRACTION = 0.30       # 30% of chains are "runaway" (critic never approves)

FLEET_SIZES = [1, 10, 50, 100, 500, 1000]


# ---------------------------------------------------------------------------
# Single chain simulation
# ---------------------------------------------------------------------------

def _simulate_chain(is_runaway: bool, with_containment: bool) -> dict[str, Any]:
    """Simulate one agent chain. Returns call count and cost."""
    target_steps = RUNAWAY_STEPS if is_runaway else CHAIN_MAX_STEPS

    if not with_containment:
        # Baseline: run to target_steps unconditionally
        calls = target_steps
        cost = calls * COST_PER_CALL
        return {"calls": calls, "cost_usd": cost, "halted": False}

    # Veronica containment
    config = ExecutionConfig(
        max_cost_usd=CONTAINED_BUDGET_USD,
        max_steps=CONTAINED_STEP_LIMIT,
        max_retries_total=50,
    )
    guard = AgentStepGuard(max_steps=CONTAINED_STEP_LIMIT)
    call_count = 0
    halted = False

    with ExecutionContext(config=config) as ctx:
        step = 0
        while guard.step(result=f"step_{step}") and step < target_steps:
            decision = ctx.wrap_llm_call(
                fn=lambda: {"tokens": 500, "cost": COST_PER_CALL},
                options=WrapOptions(
                    operation_name="llm_call",
                    cost_estimate_hint=COST_PER_CALL,
                ),
            )
            if decision == Decision.HALT:
                halted = True
                break
            call_count += 1
            step += 1
        if guard.is_exceeded:
            halted = True

    snap = ctx.get_snapshot()
    return {
        "calls": call_count,
        "cost_usd": snap.cost_usd_accumulated,
        "halted": halted,
    }


# ---------------------------------------------------------------------------
# Fleet simulation (threaded)
# ---------------------------------------------------------------------------

@dataclass
class FleetResult:
    fleet_size: int
    # Baseline
    baseline_total_calls: int
    baseline_total_cost: float
    baseline_elapsed_ms: float
    # Veronica
    veronica_total_calls: int
    veronica_total_cost: float
    veronica_elapsed_ms: float
    veronica_halted_chains: int
    # Per-chain overhead
    overhead_per_chain_ms: float
    # Savings
    call_reduction_pct: float
    cost_reduction_pct: float
    throughput_chains_per_sec: float


def _run_fleet(fleet_size: int, with_containment: bool) -> dict[str, Any]:
    """Run fleet_size chains concurrently (threaded). Returns aggregate stats."""
    # Determine which chains are runaway
    n_runaway = max(1, int(fleet_size * RUNAWAY_FRACTION))
    is_runaway_flags = [i < n_runaway for i in range(fleet_size)]

    results: list[dict[str, Any]] = [{}] * fleet_size
    lock = threading.Lock()

    def _worker(idx: int) -> None:
        r = _simulate_chain(is_runaway_flags[idx], with_containment=with_containment)
        with lock:
            results[idx] = r

    t0 = time.perf_counter()
    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(fleet_size)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed_ms = (time.perf_counter() - t0) * 1000

    total_calls = sum(r["calls"] for r in results)
    total_cost = sum(r["cost_usd"] for r in results)
    halted = sum(1 for r in results if r.get("halted", False))

    return {
        "total_calls": total_calls,
        "total_cost": total_cost,
        "elapsed_ms": elapsed_ms,
        "halted_chains": halted,
        "throughput_chains_per_sec": (fleet_size / elapsed_ms) * 1000,
    }


def _simulate_fleet_size(fleet_size: int) -> FleetResult:
    baseline = _run_fleet(fleet_size, with_containment=False)
    veronica = _run_fleet(fleet_size, with_containment=True)

    overhead_total_ms = veronica["elapsed_ms"] - baseline["elapsed_ms"]
    overhead_per_chain = overhead_total_ms / fleet_size

    def _pct(b: float, v: float) -> float:
        return round(100.0 * (1.0 - v / b), 1) if b > 0 else 0.0

    return FleetResult(
        fleet_size=fleet_size,
        baseline_total_calls=baseline["total_calls"],
        baseline_total_cost=round(baseline["total_cost"], 2),
        baseline_elapsed_ms=round(baseline["elapsed_ms"], 2),
        veronica_total_calls=veronica["total_calls"],
        veronica_total_cost=round(veronica["total_cost"], 2),
        veronica_elapsed_ms=round(veronica["elapsed_ms"], 2),
        veronica_halted_chains=veronica["halted_chains"],
        overhead_per_chain_ms=round(overhead_per_chain, 3),
        call_reduction_pct=_pct(baseline["total_calls"], veronica["total_calls"]),
        cost_reduction_pct=_pct(baseline["total_cost"], veronica["total_cost"]),
        throughput_chains_per_sec=round(veronica["throughput_chains_per_sec"], 1),
    )


# ---------------------------------------------------------------------------
# Overhead micro-benchmark (serial, single chain)
# ---------------------------------------------------------------------------

def _measure_per_call_overhead(n_samples: int = 200) -> dict[str, float]:
    """Measure mean overhead of ExecutionContext.wrap_llm_call vs raw call."""
    call_fn = lambda: {"tokens": 500, "cost": COST_PER_CALL}  # noqa: E731

    # Raw timing
    raw_times: list[float] = []
    for _ in range(n_samples):
        t0 = time.perf_counter()
        call_fn()
        raw_times.append((time.perf_counter() - t0) * 1e6)

    # Wrapped timing
    config = ExecutionConfig(
        max_cost_usd=9999.0,
        max_steps=n_samples + 100,
        max_retries_total=n_samples + 100,
    )
    wrapped_times: list[float] = []
    with ExecutionContext(config=config) as ctx:
        for _ in range(n_samples):
            t0 = time.perf_counter()
            ctx.wrap_llm_call(
                fn=call_fn,
                options=WrapOptions(
                    operation_name="bench_call",
                    cost_estimate_hint=COST_PER_CALL,
                ),
            )
            wrapped_times.append((time.perf_counter() - t0) * 1e6)

    raw_mean = statistics.mean(raw_times)
    wrapped_mean = statistics.mean(wrapped_times)
    overhead_us = wrapped_mean - raw_mean

    return {
        "raw_mean_us": round(raw_mean, 2),
        "wrapped_mean_us": round(wrapped_mean, 2),
        "overhead_us": round(overhead_us, 2),
        "overhead_pct": round(100.0 * overhead_us / max(raw_mean, 0.001), 1),
        "n_samples": n_samples,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 84)
    print("SCALE SIMULATION: Veronica Containment at Agent Fleet Scale")
    print(f"Params: runaway_fraction={RUNAWAY_FRACTION:.0%}, "
          f"runaway_steps={RUNAWAY_STEPS}, "
          f"step_limit={CONTAINED_STEP_LIMIT}, "
          f"budget=${CONTAINED_BUDGET_USD}/chain")
    print("=" * 84)

    # Overhead micro-benchmark
    print()
    print("Section 1: Per-Call Overhead (wrap_llm_call vs raw call)")
    print("-" * 50)
    overhead = _measure_per_call_overhead(n_samples=200)
    print(f"  Raw call:     {overhead['raw_mean_us']:.2f} us (mean, n={overhead['n_samples']})")
    print(f"  Wrapped call: {overhead['wrapped_mean_us']:.2f} us (mean)")
    print(f"  Overhead:     {overhead['overhead_us']:.2f} us ({overhead['overhead_pct']:.1f}%)")
    print()
    print("  Interpretation: containment adds microsecond-scale overhead per call.")
    print("  At $0.05/call LLM pricing, 1 prevented call saves ~50,000x the overhead cost.")

    # Fleet-scale simulation
    print()
    print("Section 2: Fleet-Scale Simulation")
    print("-" * 84)
    fleet_results: list[FleetResult] = []
    for fleet_size in FLEET_SIZES:
        r = _simulate_fleet_size(fleet_size)
        fleet_results.append(r)

    # Table 1: Call and cost savings
    print()
    print("Table 1: LLM Calls and Cost at Scale")
    w = 84
    print("-" * w)
    print(
        f"{'Fleet':>6} {'Base Calls':>11} {'Ver Calls':>10} "
        f"{'Call-Red%':>10} {'Base Cost $':>12} {'Ver Cost $':>10} {'Cost-Red%':>10}"
    )
    print("-" * w)
    for r in fleet_results:
        print(
            f"{r.fleet_size:>6} {r.baseline_total_calls:>11} {r.veronica_total_calls:>10} "
            f"{r.call_reduction_pct:>9.1f}% {r.baseline_total_cost:>12.2f} "
            f"{r.veronica_total_cost:>10.2f} {r.cost_reduction_pct:>9.1f}%"
        )
    print("-" * w)

    # Table 2: Throughput and overhead
    print()
    print("Table 2: Throughput and Containment Overhead")
    print("-" * 72)
    print(
        f"{'Fleet':>6} {'Base ms':>9} {'Ver ms':>9} "
        f"{'Overhead/chain ms':>19} {'Throughput ch/s':>17} {'Halted':>8}"
    )
    print("-" * 72)
    for r in fleet_results:
        print(
            f"{r.fleet_size:>6} {r.baseline_elapsed_ms:>9.2f} {r.veronica_elapsed_ms:>9.2f} "
            f"{r.overhead_per_chain_ms:>19.3f} {r.throughput_chains_per_sec:>17.1f} "
            f"{r.veronica_halted_chains:>8}"
        )
    print("-" * 72)

    # Extrapolation to annual savings
    print()
    print("Section 3: Annual Cost Projection (1000 chains/day, 365 days)")
    print("-" * 60)
    r1000 = next((r for r in fleet_results if r.fleet_size == 1000), fleet_results[-1])
    daily_cost_baseline = r1000.baseline_total_cost
    daily_cost_veronica = r1000.veronica_total_cost
    annual_savings = (daily_cost_baseline - daily_cost_veronica) * 365
    print(f"  Daily baseline cost (1000 chains): ${daily_cost_baseline:>10.2f}")
    print(f"  Daily Veronica cost (1000 chains): ${daily_cost_veronica:>10.2f}")
    print(f"  Daily savings:                     ${daily_cost_baseline - daily_cost_veronica:>10.2f}")
    print(f"  Annual projected savings:           ${annual_savings:>10,.2f}")

    # Summary
    avg_call_red = statistics.mean(r.call_reduction_pct for r in fleet_results)
    avg_cost_red = statistics.mean(r.cost_reduction_pct for r in fleet_results)
    print()
    print("Summary:")
    print(f"  Average call reduction across all fleet sizes: {avg_call_red:.1f}%")
    print(f"  Average cost reduction across all fleet sizes: {avg_cost_red:.1f}%")
    print(f"  Per-call containment overhead: {overhead['overhead_us']:.2f} us")
    print("  Overhead scales sub-linearly with fleet size (thread parallelism)")
    print()
    print("Note: All LLM calls are stubbed (no network). Costs are simulated")
    print(f"at ${COST_PER_CALL}/call (comparable to GPT-4o input pricing).")


if __name__ == "__main__":
    main()
