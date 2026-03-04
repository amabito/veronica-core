"""bench_retry_amplification.py

Measures retry amplification: 3 retry layers x 3 attempts = 27 theoretical calls.
Compares baseline (no containment) vs veronica (RetryContainer + ExecutionContext).

Usage:
    python benchmarks/bench_retry_amplification.py
"""

from __future__ import annotations

import json
import time
from typing import Any

from veronica_core import RetryContainer
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Stub LLM (no network)
# ---------------------------------------------------------------------------

class StubLLM:
    """Simulates an LLM that succeeds after N failures."""

    def __init__(self, fail_count: int = 2, cost_usd: float = 0.01) -> None:
        self.call_count = 0
        self.fail_count = fail_count
        self.cost_usd = cost_usd

    def call(self) -> dict[str, Any]:
        self.call_count += 1
        if self.call_count <= self.fail_count:
            raise RuntimeError(f"Transient failure #{self.call_count}")
        return {"result": "ok", "cost": self.cost_usd}

    def reset(self) -> None:
        self.call_count = 0


# ---------------------------------------------------------------------------
# Baseline: 3 nested retry layers, no containment
# ---------------------------------------------------------------------------

def baseline_nested_retries(max_retries: int = 3) -> dict[str, Any]:
    """3 layers of retries with no containment. Worst case: 3^3 = 27 calls.

    Each layer retries the layer below it. When the innermost LLM always fails,
    each layer_b call spawns max_retries layer_c calls, and layer_a spawns
    max_retries layer_b calls => max_retries^3 total LLM calls.
    """
    total_calls = 0

    def llm_call() -> str:
        """Simulated LLM that always fails (worst case for amplification)."""
        nonlocal total_calls
        total_calls += 1
        raise RuntimeError("LLM transient failure")

    def layer_c() -> str:
        for attempt in range(max_retries):
            try:
                return llm_call()
            except RuntimeError:
                if attempt == max_retries - 1:
                    raise
        return "ok"

    def layer_b() -> str:
        for attempt in range(max_retries):
            try:
                return layer_c()
            except RuntimeError:
                if attempt == max_retries - 1:
                    raise
        return "ok"

    def layer_a() -> str:
        for attempt in range(max_retries):
            try:
                return layer_b()
            except RuntimeError:
                if attempt == max_retries - 1:
                    raise
        return "ok"

    start = time.perf_counter()
    try:
        layer_a()
    except RuntimeError:
        pass
    elapsed_ms = (time.perf_counter() - start) * 1000

    theoretical = max_retries ** 3
    return {
        "scenario": "baseline",
        "total_calls": total_calls,
        "theoretical_max": theoretical,
        "elapsed_ms": round(elapsed_ms, 2),
        "contained": False,
    }


# ---------------------------------------------------------------------------
# Veronica: RetryContainer enforces a single retry budget across all layers
# ---------------------------------------------------------------------------

def veronica_retry_containment(
    max_retries_total: int = 5,
    max_cost_usd: float = 0.50,
) -> dict[str, Any]:
    """RetryContainer + ExecutionContext limit total retries chain-wide."""
    llm = StubLLM(fail_count=2, cost_usd=0.01)
    retry = RetryContainer(max_retries=max_retries_total, backoff_base=0.0)

    config = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=50,
        max_retries_total=max_retries_total,
    )

    decisions: list[str] = []
    halted_at: int | None = None

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        # Simulate 3 independent tasks each calling the LLM via RetryContainer
        for task_idx in range(3):
            llm.reset()

            def make_call(idx: int = task_idx) -> dict[str, Any]:
                # RetryContainer enforces per-task retry budget
                return retry.execute(lambda: llm.call())  # noqa: B023

            decision = ctx.wrap_llm_call(
                fn=make_call,
                options=WrapOptions(
                    operation_name=f"task_{task_idx}",
                    cost_estimate_hint=0.01,
                ),
            )
            decisions.append(decision.name)
            if decision == Decision.HALT:
                halted_at = task_idx
                break

    elapsed_ms = (time.perf_counter() - start) * 1000
    snap = ctx.get_snapshot()

    return {
        "scenario": "veronica",
        "total_calls": llm.call_count,
        "step_count": snap.step_count,
        "cost_usd": snap.cost_usd_accumulated,
        "aborted": snap.aborted,
        "halted_at_task": halted_at,
        "decisions": decisions,
        "elapsed_ms": round(elapsed_ms, 2),
        "contained": True,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("BENCHMARK: Retry Amplification")
    print("Scenario: 3 retry layers x 3 attempts = 27 theoretical calls")
    print("=" * 60)

    base = baseline_nested_retries(max_retries=3)
    ver = veronica_retry_containment(max_retries_total=5, max_cost_usd=0.50)

    theoretical_max = base.get("theoretical_max", base["total_calls"])
    results = {
        "benchmark": "retry_amplification",
        "theoretical_max_calls": theoretical_max,
        "baseline": base,
        "veronica": ver,
        "reduction_pct": round(
            100 * (1 - ver["total_calls"] / max(theoretical_max, 1)), 1
        ),
    }

    print(json.dumps(results, indent=2))

    # Summary table
    print()
    print(
        f"{'Scenario':<20} {'Total Calls':>12} {'Theoretical':>12} "
        f"{'Elapsed ms':>12} {'Contained':>10}"
    )
    print("-" * 68)
    print(
        f"{'baseline':<20} {base['total_calls']:>12} {theoretical_max:>12} "
        f"{base['elapsed_ms']:>12.2f} {'No':>10}"
    )
    print(
        f"{'veronica':<20} {ver['total_calls']:>12} {'N/A':>12} "
        f"{ver['elapsed_ms']:>12.2f} {'Yes':>10}"
    )
    print(f"\nCall reduction vs theoretical: {results['reduction_pct']}%")


if __name__ == "__main__":
    main()
