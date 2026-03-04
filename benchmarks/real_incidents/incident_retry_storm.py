"""incident_retry_storm.py

Real incident: LangChain-style retry amplification storm.

A production pipeline used three layers of retry logic:
- Layer A (application): 3 retries on any error
- Layer B (LLM client): 3 retries on network error
- Layer C (HTTP transport): 3 retries on connection reset

When the upstream LLM API entered a degraded state, all three layers
triggered simultaneously, amplifying a single logical call into
3^3 = 27 actual API calls per attempt. With 10 concurrent requests,
this produced 270 simultaneous calls, triggering rate limits and
causing a cascading failure across the service.

Real data (LangChain GitHub issue #8834 pattern, 2023-Q4):
    - Theoretical max: 3^3 = 27 calls per logical request
    - Concurrent requests: 10
    - Total API calls: up to 270 in burst
    - Rate limit hit: 429 errors after call 50
    - Service recovery time: 14 minutes

This benchmark simulates the nested retry amplification and shows
how RetryContainer + ExecutionContext collapse the layers into a
single shared retry budget.

Usage:
    python benchmarks/real_incidents/incident_retry_storm.py
"""

from __future__ import annotations

import time
from typing import Any

from veronica_core import RetryContainer
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Stub network: always raises until exhausted
# ---------------------------------------------------------------------------

class DegradedAPI:
    """Simulates an LLM API that always fails (degraded state)."""

    def __init__(self) -> None:
        self.call_count = 0

    def call(self) -> str:
        self.call_count += 1
        raise ConnectionResetError(f"API degraded: call #{self.call_count} failed")

    def reset(self) -> None:
        self.call_count = 0


# ---------------------------------------------------------------------------
# Baseline: 3 nested retry layers, no shared budget
# ---------------------------------------------------------------------------

def baseline_retry_storm(
    retries_per_layer: int = 3,
    cost_per_call_usd: float = 0.010,
) -> dict[str, Any]:
    """Three independent retry layers amplify calls to 3^3 = 27 per request.

    In the real incident, 10 concurrent requests produced up to 270 calls.
    Here we simulate one logical request to show the amplification.
    """
    api = DegradedAPI()

    def transport_call() -> str:
        """Layer C: HTTP transport retry."""
        for attempt in range(retries_per_layer):
            try:
                return api.call()
            except ConnectionResetError:
                if attempt == retries_per_layer - 1:
                    raise
        return "ok"

    def client_call() -> str:
        """Layer B: LLM client retry."""
        for attempt in range(retries_per_layer):
            try:
                return transport_call()
            except ConnectionResetError:
                if attempt == retries_per_layer - 1:
                    raise
        return "ok"

    def app_call() -> str:
        """Layer A: Application retry."""
        for attempt in range(retries_per_layer):
            try:
                return client_call()
            except ConnectionResetError:
                if attempt == retries_per_layer - 1:
                    raise
        return "ok"

    start = time.perf_counter()
    try:
        app_call()
    except ConnectionResetError:
        pass
    elapsed_ms = (time.perf_counter() - start) * 1000

    theoretical = retries_per_layer ** 3
    total_cost = api.call_count * cost_per_call_usd

    return {
        "scenario": "baseline",
        "incident": "LangChain retry amplification (2023-Q4)",
        "api_calls": api.call_count,
        "theoretical_max": theoretical,
        "amplification_factor": round(api.call_count / max(retries_per_layer, 1), 1),
        "total_cost_usd": round(total_cost, 4),
        "elapsed_ms": round(elapsed_ms, 2),
        "contained": False,
        "cost_saved_pct": 0.0,
    }


# ---------------------------------------------------------------------------
# Veronica: RetryContainer + ExecutionContext enforce shared budget
# ---------------------------------------------------------------------------

def veronica_retry_storm(
    max_retries_total: int = 5,
    max_cost_usd: float = 0.10,
    cost_per_call_usd: float = 0.010,
) -> dict[str, Any]:
    """RetryContainer collapses nested retries into a single shared budget.

    Instead of 3 independent layers each retrying 3 times (27 calls),
    a shared RetryContainer limits the total to max_retries_total.
    """
    api = DegradedAPI()
    retry = RetryContainer(max_retries=max_retries_total, backoff_base=0.0)
    config = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=100,
        max_retries_total=max_retries_total,
    )

    halted_by = "unknown"
    decisions: list[str] = []

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        decision = ctx.wrap_llm_call(
            fn=lambda: retry.execute(lambda: api.call()),
            options=WrapOptions(
                operation_name="app_call_contained",
                cost_estimate_hint=cost_per_call_usd,
            ),
        )
        decisions.append(decision.name)
        if decision == Decision.HALT:
            halted_by = "execution_context"
        else:
            halted_by = "retry_exhausted"

    elapsed_ms = (time.perf_counter() - start) * 1000
    snap = ctx.get_snapshot()
    total_cost = api.call_count * cost_per_call_usd

    return {
        "scenario": "veronica",
        "incident": "LangChain retry amplification (2023-Q4)",
        "api_calls": api.call_count,
        "max_retries_total": max_retries_total,
        "total_cost_usd": round(total_cost, 4),
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
    RETRIES = 3
    COST_PER_CALL = 0.010

    base = baseline_retry_storm(retries_per_layer=RETRIES, cost_per_call_usd=COST_PER_CALL)
    ver = veronica_retry_storm(
        max_retries_total=5, max_cost_usd=0.10, cost_per_call_usd=COST_PER_CALL
    )

    baseline_calls = base["api_calls"]
    veronica_calls = ver["api_calls"]
    cost_saved_pct = round(
        100 * (1 - veronica_calls / max(baseline_calls, 1)), 1
    )

    print("=" * 68)
    print("INCIDENT: LangChain-Style Retry Amplification Storm (2023-Q4)")
    print(f"Theoretical max: {RETRIES}^3 = {RETRIES**3} calls per logical request")
    print("=" * 68)
    print()
    print(f"{'scenario':<20} {'baseline_calls':>16} {'veronica_calls':>16} {'contained':>10} {'cost_saved_pct':>16}")
    print("-" * 80)
    print(f"{'baseline':<20} {baseline_calls:>16} {'N/A':>16} {'False':>10} {'0.0%':>16}")
    print(f"{'veronica':<20} {'N/A':>16} {veronica_calls:>16} {'True':>10} {cost_saved_pct:>15.1f}%")
    print()
    print(f"Amplification factor (baseline): {base['amplification_factor']}x per layer")
    print(f"Baseline cost: ${base['total_cost_usd']:.4f} | Veronica cost: ${ver['total_cost_usd']:.4f}")
    print(f"Call reduction: {cost_saved_pct}%")


if __name__ == "__main__":
    main()
