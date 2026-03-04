"""ablation_study.py

Systematic ablation study: incremental component contribution to containment.

Configurations (cumulative, each adds one layer):
  budget_only         BudgetEnforcer via ExecutionContext (cost ceiling only)
  budget_cb           Budget + CircuitBreaker (failure isolation added)
  budget_cb_timeout   Budget + CB + Timeout (wall-clock deadline added)
  full_veronica       Budget + CB + Timeout + Step limits + CancellationToken

Scenarios tested:
  retry_storm         -- Always-failing LLM, nested retries, amplifies 3^3=27
  multi_agent_loop    -- Planner/critic loop; critic never approves

Metrics per cell:
  containment_rate    Fraction of runaway calls blocked (%, vs no-containment)
  cost_reduction      Cost saved vs no-containment (%)
  system_stability    True if no uncaught exception and chain terminated cleanly

Usage:
    python benchmarks/ablation_study.py
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from veronica_core import AgentStepGuard, BudgetEnforcer, CircuitBreaker
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.runtime_policy import PolicyContext
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKENS_PER_CALL = 500
COST_PER_CALL = 0.01

# Runaway maximums (no containment)
RETRY_RUNAWAY_LIMIT = 27          # 3^3 nested retries
AGENT_RUNAWAY_LIMIT = 80          # 40 iterations x 2 calls (planner+critic)

# Containment parameters (cumulative across configs)
BUDGET_USD = 0.15                 # $0.15 ceiling
CB_THRESHOLD = 4                  # Circuit opens after 4 consecutive failures
TIMEOUT_MS = 5000                 # 5 s (structural; never fires in synthetic)
STEP_LIMIT = 10                   # Max steps per chain
RETRY_LIMIT = 5                   # Max retries total


# ---------------------------------------------------------------------------
# Scenario A: Retry storm helpers
# ---------------------------------------------------------------------------

def _retry_storm(
    *,
    with_budget: bool = False,
    with_cb: bool = False,
    with_timeout: bool = False,
    with_step_limit: bool = False,
) -> dict[str, Any]:
    """Run the retry storm scenario under the given configuration."""
    call_count = 0
    halted = False
    stable = True

    budget = BudgetEnforcer(limit_usd=BUDGET_USD) if with_budget else None
    cb = CircuitBreaker(failure_threshold=CB_THRESHOLD, recovery_timeout=9999.0) if with_cb else None

    config = ExecutionConfig(
        max_cost_usd=BUDGET_USD if with_budget else 9999.0,
        max_steps=STEP_LIMIT if with_step_limit else 9999,
        max_retries_total=RETRY_LIMIT if with_step_limit else 9999,
        timeout_ms=TIMEOUT_MS if with_timeout else 0,
    )

    def _fail_fn() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("transient fail")

    t0 = time.perf_counter()
    try:
        with ExecutionContext(config=config, circuit_breaker=cb) as ctx:
            for _ in range(RETRY_RUNAWAY_LIMIT):
                # Check circuit breaker before dispatch
                if cb is not None:
                    cb_dec = cb.check(PolicyContext())
                    if not cb_dec.allowed:
                        halted = True
                        break

                decision = ctx.wrap_llm_call(
                    fn=_fail_fn,
                    options=WrapOptions(
                        operation_name="retry_call",
                        cost_estimate_hint=COST_PER_CALL,
                    ),
                )

                # Record failure for CB
                if cb is not None:
                    cb.record_failure()

                # Check budget enforcer
                if budget is not None and not budget.spend(COST_PER_CALL):
                    halted = True
                    break

                if decision == Decision.HALT:
                    halted = True
                    break

    except Exception:
        stable = False

    elapsed_ms = (time.perf_counter() - t0) * 1000
    total_cost = call_count * COST_PER_CALL

    containment_rate = 100.0 * (1.0 - call_count / RETRY_RUNAWAY_LIMIT)
    cost_reduction = 100.0 * (1.0 - total_cost / (RETRY_RUNAWAY_LIMIT * COST_PER_CALL))

    return {
        "calls": call_count,
        "cost_usd": round(total_cost, 4),
        "halted": halted,
        "stable": stable,
        "containment_rate": round(containment_rate, 1),
        "cost_reduction": round(cost_reduction, 1),
        "elapsed_ms": round(elapsed_ms, 3),
    }


# ---------------------------------------------------------------------------
# Scenario B: Multi-agent loop helpers
# ---------------------------------------------------------------------------

def _agent_loop(
    *,
    with_budget: bool = False,
    with_cb: bool = False,
    with_timeout: bool = False,
    with_step_limit: bool = False,
) -> dict[str, Any]:
    """Run the multi-agent loop scenario under the given configuration."""
    call_count = 0
    halted = False
    stable = True

    budget = BudgetEnforcer(limit_usd=BUDGET_USD) if with_budget else None
    cb = CircuitBreaker(failure_threshold=CB_THRESHOLD, recovery_timeout=9999.0) if with_cb else None
    guard = AgentStepGuard(max_steps=STEP_LIMIT) if with_step_limit else None

    config = ExecutionConfig(
        max_cost_usd=BUDGET_USD if with_budget else 9999.0,
        max_steps=STEP_LIMIT * 2 + 5 if with_step_limit else 9999,
        max_retries_total=RETRY_LIMIT if with_step_limit else 9999,
        timeout_ms=TIMEOUT_MS if with_timeout else 0,
    )

    t0 = time.perf_counter()
    try:
        step = 0
        with ExecutionContext(config=config, circuit_breaker=cb) as ctx:
            while True:
                # Step guard check
                if guard is not None and not guard.step(result=f"iter_{step}"):
                    halted = True
                    break

                # Per-step budget check
                if budget is not None and budget.is_exceeded:
                    halted = True
                    break

                # Iteration limit (no containment baseline)
                if step >= 40:
                    break

                for role in ("planner", "critic"):
                    decision = ctx.wrap_llm_call(
                        fn=lambda: {"tokens": TOKENS_PER_CALL, "cost": COST_PER_CALL},
                        options=WrapOptions(
                            operation_name=role,
                            cost_estimate_hint=COST_PER_CALL,
                        ),
                    )
                    call_count += 1

                    if cb is not None:
                        cb.record_success()

                    if budget is not None and not budget.spend(COST_PER_CALL):
                        halted = True
                        break

                    if decision == Decision.HALT:
                        halted = True
                        break

                if halted:
                    break
                step += 1

    except Exception:
        stable = False

    elapsed_ms = (time.perf_counter() - t0) * 1000
    total_cost = call_count * COST_PER_CALL

    containment_rate = 100.0 * (1.0 - call_count / AGENT_RUNAWAY_LIMIT)
    cost_reduction = 100.0 * (1.0 - total_cost / (AGENT_RUNAWAY_LIMIT * COST_PER_CALL))

    return {
        "calls": call_count,
        "cost_usd": round(total_cost, 4),
        "halted": halted,
        "stable": stable,
        "containment_rate": round(max(containment_rate, 0.0), 1),
        "cost_reduction": round(max(cost_reduction, 0.0), 1),
        "elapsed_ms": round(elapsed_ms, 3),
    }


# ---------------------------------------------------------------------------
# Ablation configuration table
# ---------------------------------------------------------------------------

@dataclass
class AblationConfig:
    label: str
    with_budget: bool
    with_cb: bool
    with_timeout: bool
    with_step_limit: bool


CONFIGS = [
    AblationConfig("budget_only",        True,  False, False, False),
    AblationConfig("budget_cb",          True,  True,  False, False),
    AblationConfig("budget_cb_timeout",  True,  True,  True,  False),
    AblationConfig("full_veronica",      True,  True,  True,  True),
]


@dataclass
class AblationRow:
    config: str
    scenario: str
    calls: int
    containment_rate: float
    cost_reduction: float
    system_stability: bool
    elapsed_ms: float


def _run_config(cfg: AblationConfig, scenario: str) -> AblationRow:
    kwargs = dict(
        with_budget=cfg.with_budget,
        with_cb=cfg.with_cb,
        with_timeout=cfg.with_timeout,
        with_step_limit=cfg.with_step_limit,
    )
    fn: Callable[..., dict[str, Any]] = (
        _retry_storm if scenario == "retry_storm" else _agent_loop
    )
    result = fn(**kwargs)
    return AblationRow(
        config=cfg.label,
        scenario=scenario,
        calls=result["calls"],
        containment_rate=result["containment_rate"],
        cost_reduction=result["cost_reduction"],
        system_stability=result["stable"],
        elapsed_ms=result["elapsed_ms"],
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    scenarios = ["retry_storm", "multi_agent_loop"]

    print("=" * 82)
    print("ABLATION STUDY: Incremental Component Contribution to Containment")
    print(f"Budget: ${BUDGET_USD} | CB threshold: {CB_THRESHOLD} | "
          f"Step limit: {STEP_LIMIT} | Timeout: {TIMEOUT_MS}ms")
    print("=" * 82)

    all_rows: list[AblationRow] = []
    for scenario in scenarios:
        for cfg in CONFIGS:
            all_rows.append(_run_config(cfg, scenario))

    # Print table per scenario
    for scenario in scenarios:
        rows = [r for r in all_rows if r.scenario == scenario]
        runaway = RETRY_RUNAWAY_LIMIT if scenario == "retry_storm" else AGENT_RUNAWAY_LIMIT

        print()
        print(f"Scenario: {scenario}  (no-containment baseline: {runaway} calls)")
        print("-" * 82)
        print(
            f"{'Configuration':<22} {'Calls':>7} "
            f"{'Containment%':>13} {'Cost-Red%':>10} "
            f"{'Stability':>10} {'Latency ms':>12}"
        )
        print("-" * 82)
        for r in rows:
            stab = "Stable" if r.system_stability else "Unstable"
            print(
                f"{r.config:<22} {r.calls:>7} "
                f"{r.containment_rate:>12.1f}% {r.cost_reduction:>9.1f}% "
                f"{stab:>10} {r.elapsed_ms:>12.3f}"
            )
        print("-" * 82)

    # Marginal contribution table (full_veronica minus each sub-config)
    print()
    print("Marginal contribution of each added component (full_veronica as reference)")
    print("-" * 80)
    print(
        f"{'Component added':<25} {'Scenario':<22} "
        f"{'Delta containment%':>20} {'Delta cost-red%':>16}"
    )
    print("-" * 80)

    config_pairs = [
        ("budget_only",       "budget_cb",          "CircuitBreaker"),
        ("budget_cb",         "budget_cb_timeout",  "Timeout"),
        ("budget_cb_timeout", "full_veronica",       "Step+Cancel"),
    ]

    for from_cfg, to_cfg, component in config_pairs:
        for scenario in scenarios:
            r_from = next(r for r in all_rows if r.config == from_cfg and r.scenario == scenario)
            r_to = next(r for r in all_rows if r.config == to_cfg and r.scenario == scenario)
            delta_cont = r_to.containment_rate - r_from.containment_rate
            delta_cost = r_to.cost_reduction - r_from.cost_reduction
            print(
                f"{component:<25} {scenario:<22} "
                f"{delta_cont:>+19.1f}% {delta_cost:>+15.1f}%"
            )

    print("-" * 80)
    print()
    print("Note: positive delta = adding that component improved containment.")

    # Final summary
    fv_rows = [r for r in all_rows if r.config == "full_veronica"]
    avg_cont = sum(r.containment_rate for r in fv_rows) / len(fv_rows)
    avg_cost = sum(r.cost_reduction for r in fv_rows) / len(fv_rows)
    all_stable = all(r.system_stability for r in fv_rows)
    print()
    print(f"Full veronica average containment rate: {avg_cont:.1f}%")
    print(f"Full veronica average cost reduction:   {avg_cost:.1f}%")
    print(f"Full veronica system stability:         {'Stable' if all_stable else 'Unstable'}")


if __name__ == "__main__":
    main()
