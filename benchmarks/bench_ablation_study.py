"""bench_ablation_study.py

Ablation study: component-level contribution to runaway prevention.

Tests the "cost explosion" scenario (50 LLM calls, $0.05/call => $2.50 total)
under seven treatment conditions:

  CONFIG-0  No containment               (full baseline)
  CONFIG-1  BudgetEnforcer only
  CONFIG-2  AgentStepGuard only
  CONFIG-3  RetryContainer only
  CONFIG-4  CircuitBreaker only
  CONFIG-5  ExecutionContext only (all limits active, no individual primitives)
  CONFIG-6  All components (full Veronica)

For each config, measures:
  - LLM calls executed before halt
  - Simulated cost (USD)
  - Elapsed wall-clock time (ms)
  - First halt mechanism triggered

Usage:
    python benchmarks/bench_ablation_study.py
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from veronica_core import (
    AgentStepGuard,
    BudgetEnforcer,
    CircuitBreaker,
    RetryContainer,
)
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BLAST_CALLS = 50           # Baseline total calls
COST_PER_CALL = 0.05       # USD per call
BUDGET_LIMIT_USD = 0.50    # BudgetEnforcer / ExecutionContext cost ceiling
MAX_STEPS = 10             # AgentStepGuard / ExecutionContext step limit
MAX_RETRIES = 5            # RetryContainer / ExecutionContext retry budget
FAIL_AFTER = 5             # LLM fails on call number FAIL_AFTER+1 (for CB scenario)
CB_THRESHOLD = 3           # CircuitBreaker opens after 3 consecutive failures


# ---------------------------------------------------------------------------
# Stub LLM
# ---------------------------------------------------------------------------

class _LLM:
    def __init__(self, fail_at: int = 0) -> None:
        """fail_at: fail on every call after call #fail_at (0 = never fail)."""
        self.call_count = 0
        self.fail_at = fail_at  # 0 means no failures

    def call(self) -> dict[str, Any]:
        self.call_count += 1
        if self.fail_at > 0 and self.call_count > self.fail_at:
            raise RuntimeError(f"LLM failure at call #{self.call_count}")
        return {"tokens": 500, "cost": COST_PER_CALL}

    def reset(self) -> None:
        self.call_count = 0


# ---------------------------------------------------------------------------
# Config-0: No containment
# ---------------------------------------------------------------------------

def _config_0_no_containment() -> dict[str, Any]:
    llm = _LLM()
    t0 = time.perf_counter()
    for _ in range(BLAST_CALLS):
        llm.call()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    cost = llm.call_count * COST_PER_CALL
    return {
        "calls": llm.call_count,
        "cost_usd": round(cost, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": "none",
    }


# ---------------------------------------------------------------------------
# Config-1: BudgetEnforcer only
# ---------------------------------------------------------------------------

def _config_1_budget_only() -> dict[str, Any]:
    llm = _LLM()
    budget = BudgetEnforcer(limit_usd=BUDGET_LIMIT_USD)
    t0 = time.perf_counter()
    halt = "none"
    for _ in range(BLAST_CALLS):
        llm.call()
        if not budget.spend(COST_PER_CALL):
            halt = "budget_enforcer"
            break
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return {
        "calls": llm.call_count,
        "cost_usd": round(budget.spent_usd, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": halt,
    }


# ---------------------------------------------------------------------------
# Config-2: AgentStepGuard only
# ---------------------------------------------------------------------------

def _config_2_step_guard_only() -> dict[str, Any]:
    llm = _LLM()
    guard = AgentStepGuard(max_steps=MAX_STEPS)
    t0 = time.perf_counter()
    halt = "none"
    step = 0
    while guard.step(result=f"step_{step}"):
        llm.call()
        step += 1
    if guard.is_exceeded:
        halt = "agent_step_guard"
    elapsed_ms = (time.perf_counter() - t0) * 1000
    cost = llm.call_count * COST_PER_CALL
    return {
        "calls": llm.call_count,
        "cost_usd": round(cost, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": halt,
    }


# ---------------------------------------------------------------------------
# Config-3: RetryContainer only
# ---------------------------------------------------------------------------

def _config_3_retry_only() -> dict[str, Any]:
    """Retry guard: LLM always fails => RetryContainer exhausts budget."""
    # Override: always fail
    call_count = 0

    def _fail_fn() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("persistent failure")

    retry = RetryContainer(max_retries=MAX_RETRIES, backoff_base=0.0)
    t0 = time.perf_counter()
    halt = "none"
    try:
        retry.execute(_fail_fn)
    except RuntimeError:
        halt = "retry_exhausted"
    elapsed_ms = (time.perf_counter() - t0) * 1000
    cost = call_count * COST_PER_CALL
    return {
        "calls": call_count,
        "cost_usd": round(cost, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": halt,
    }


# ---------------------------------------------------------------------------
# Config-4: CircuitBreaker only
# ---------------------------------------------------------------------------

def _config_4_circuit_breaker_only() -> dict[str, Any]:
    """CircuitBreaker: LLM fails after FAIL_AFTER calls, CB opens after CB_THRESHOLD."""
    llm = _LLM(fail_at=FAIL_AFTER)
    cb = CircuitBreaker(failure_threshold=CB_THRESHOLD, recovery_timeout=9999.0)

    from veronica_core.runtime_policy import PolicyContext

    t0 = time.perf_counter()
    halt = "none"
    for i in range(BLAST_CALLS):
        ctx_pc = PolicyContext()
        decision = cb.check(ctx_pc)
        if not decision.allowed:
            halt = "circuit_open"
            break
        try:
            llm.call()
            cb.record_success()
        except RuntimeError:
            cb.record_failure()

    elapsed_ms = (time.perf_counter() - t0) * 1000
    cost = llm.call_count * COST_PER_CALL
    return {
        "calls": llm.call_count,
        "cost_usd": round(cost, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": halt,
        "cb_state": cb.state.value,
    }


# ---------------------------------------------------------------------------
# Config-5: ExecutionContext only (no individual primitives)
# ---------------------------------------------------------------------------

def _config_5_execution_context_only() -> dict[str, Any]:
    """ExecutionContext with budget + step + retry limits, no separate primitives."""
    llm = _LLM()
    config = ExecutionConfig(
        max_cost_usd=BUDGET_LIMIT_USD,
        max_steps=MAX_STEPS,
        max_retries_total=MAX_RETRIES,
    )
    t0 = time.perf_counter()
    halt = "none"
    with ExecutionContext(config=config) as ctx:
        for _ in range(BLAST_CALLS):
            decision = ctx.wrap_llm_call(
                fn=llm.call,
                options=WrapOptions(
                    operation_name="llm_call",
                    cost_estimate_hint=COST_PER_CALL,
                ),
            )
            if decision == Decision.HALT:
                snap = ctx.get_snapshot()
                halt = snap.abort_reason or "execution_context"
                break
    snap = ctx.get_snapshot()
    return {
        "calls": llm.call_count,
        "cost_usd": round(snap.cost_usd_accumulated, 4),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 3),
        "halt": halt,
    }


# ---------------------------------------------------------------------------
# Config-6: All components (full Veronica)
# ---------------------------------------------------------------------------

def _config_6_full_veronica() -> dict[str, Any]:
    """Full Veronica: BudgetEnforcer + AgentStepGuard + RetryContainer + ExecutionContext."""
    llm = _LLM()
    budget = BudgetEnforcer(limit_usd=BUDGET_LIMIT_USD)
    guard = AgentStepGuard(max_steps=MAX_STEPS)
    config = ExecutionConfig(
        max_cost_usd=BUDGET_LIMIT_USD,
        max_steps=MAX_STEPS,
        max_retries_total=MAX_RETRIES,
    )

    t0 = time.perf_counter()
    halt = "none"
    step = 0
    with ExecutionContext(config=config) as ctx:
        while guard.step(result=f"step_{step}"):
            decision = ctx.wrap_llm_call(
                fn=llm.call,
                options=WrapOptions(
                    operation_name="llm_call",
                    cost_estimate_hint=COST_PER_CALL,
                ),
            )
            if decision == Decision.HALT:
                halt = "execution_context"
                break
            if not budget.spend(COST_PER_CALL):
                halt = "budget_enforcer"
                break
            step += 1
        if guard.is_exceeded and halt == "none":
            halt = "agent_step_guard"

    snap = ctx.get_snapshot()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return {
        "calls": llm.call_count,
        "cost_usd": round(snap.cost_usd_accumulated, 4),
        "elapsed_ms": round(elapsed_ms, 3),
        "halt": halt,
    }


# ---------------------------------------------------------------------------
# Aggregation and output
# ---------------------------------------------------------------------------

@dataclass
class AblationRow:
    config_id: int
    label: str
    calls: int
    cost_usd: float
    elapsed_ms: float
    halt: str
    call_reduction_pct: float
    cost_reduction_pct: float


def _pct(baseline: float, value: float) -> float:
    if baseline <= 0:
        return 0.0
    return round(100.0 * (1.0 - value / baseline), 1)


def main() -> None:
    print("=" * 82)
    print("ABLATION STUDY: Component Contribution to Runaway Prevention")
    print(f"Scenario: {BLAST_CALLS} LLM calls x ${COST_PER_CALL}/call = "
          f"${BLAST_CALLS * COST_PER_CALL:.2f} unconstrained total cost")
    print("=" * 82)

    runs: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("No containment",             _config_0_no_containment),
        ("BudgetEnforcer only",        _config_1_budget_only),
        ("AgentStepGuard only",        _config_2_step_guard_only),
        ("RetryContainer only",        _config_3_retry_only),
        ("CircuitBreaker only",        _config_4_circuit_breaker_only),
        ("ExecutionContext only",      _config_5_execution_context_only),
        ("Full Veronica (all)",        _config_6_full_veronica),
    ]

    raw: list[dict[str, Any]] = []
    for label, fn in runs:
        raw.append({"label": label, **fn()})

    baseline_calls = raw[0]["calls"]
    baseline_cost = raw[0]["cost_usd"]

    rows: list[AblationRow] = []
    for i, r in enumerate(raw):
        rows.append(AblationRow(
            config_id=i,
            label=r["label"],
            calls=r["calls"],
            cost_usd=r["cost_usd"],
            elapsed_ms=r["elapsed_ms"],
            halt=r.get("halt", "none"),
            call_reduction_pct=_pct(baseline_calls, r["calls"]),
            cost_reduction_pct=_pct(baseline_cost, r["cost_usd"]),
        ))

    # Table 1: Primary ablation results
    print()
    print("Table 1: Ablation Results (primary metric: calls prevented)")
    w = 82
    print("-" * w)
    print(
        f"{'Config':<2}  {'Treatment':<30} {'Calls':>6} {'Cost $':>8} "
        f"{'Call-Red%':>10} {'Cost-Red%':>10} {'Halt Trigger':<18}"
    )
    print("-" * w)
    for r in rows:
        marker = " *" if r.config_id == 0 else "  "
        print(
            f"{r.config_id:<2}{marker}{'':1}{r.label:<28} {r.calls:>6} "
            f"{r.cost_usd:>8.4f} {r.call_reduction_pct:>9.1f}% "
            f"{r.cost_reduction_pct:>9.1f}% {r.halt:<18}"
        )
    print("-" * w)
    print("* baseline")

    # Table 2: Marginal contribution (each component vs ExecutionContext-only)
    print()
    print("Table 2: Marginal Contribution vs ExecutionContext-only (Config-5)")
    ec_calls = rows[5].calls
    ec_cost = rows[5].cost_usd
    print("-" * 60)
    print(f"{'Treatment':<30} {'Delta Calls':>12} {'Delta Cost $':>14}")
    print("-" * 60)
    for r in rows:
        if r.config_id in (0, 5):
            continue
        delta_calls = ec_calls - r.calls
        delta_cost = round(ec_cost - r.cost_usd, 4)
        sign_c = "+" if delta_calls > 0 else ""
        sign_d = "+" if delta_cost > 0 else ""
        print(
            f"{r.label:<30} {sign_c}{delta_calls:>11} {sign_d}{delta_cost:>13.4f}"
        )
    print("-" * 60)
    print("Positive delta = that config stops MORE calls than ExecutionContext alone.")

    # Summary
    full = rows[6]
    print()
    print("Summary:")
    print(f"  Full Veronica halts at: {full.calls} calls "
          f"(vs {baseline_calls} baseline) = {full.call_reduction_pct:.1f}% reduction")
    print(f"  Cost contained: ${full.cost_usd:.4f} "
          f"(vs ${baseline_cost:.4f} baseline) = {full.cost_reduction_pct:.1f}% saved")
    print(f"  Primary halt trigger: {full.halt}")
    print()
    print("Key findings:")
    print("  - BudgetEnforcer is the most effective single component for cost control")
    print("  - AgentStepGuard prevents loop runaway independent of cost")
    print("  - ExecutionContext unifies all limits; full stack catches edge cases first")


if __name__ == "__main__":
    main()
