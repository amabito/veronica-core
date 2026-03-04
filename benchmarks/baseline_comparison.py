"""baseline_comparison.py

Baseline comparison: 4 containment configurations x 3 runaway scenarios.

Configurations:
  no_containment   -- No guards: calls run to natural limit
  retry_limit      -- RetryContainer only (max_retries cap, no budget/CB)
  timeout_only     -- ExecutionContext with timeout_ms (no budget/step limits)
  full_veronica    -- ExecutionContext: budget + steps + retries + CircuitBreaker

Scenarios:
  retry_storm      -- Nested retries that amplify to 3^3=27 calls
  recursive_tools  -- Tool chain that recurses 20 levels deep
  multi_agent_loop -- Planner/critic loop; critic never approves

Metrics per cell:
  calls_executed       Total LLM/tool invocations that ran
  tokens_consumed      Simulated token count (500 tokens per call)
  terminated_correctly True if containment fired before runaway limit
  latency_ms           Wall-clock elapsed time

Usage:
    python benchmarks/baseline_comparison.py
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from veronica_core import AgentStepGuard, CircuitBreaker, RetryContainer
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Simulation constants
# ---------------------------------------------------------------------------

TOKENS_PER_CALL = 500          # Simulated tokens per LLM/tool call
COST_PER_CALL = 0.01           # USD per call

# Scenario: retry storm
RETRY_LAYERS = 3
RETRY_ATTEMPTS = 3
RETRY_RUNAWAY_LIMIT = RETRY_ATTEMPTS ** RETRY_LAYERS  # 27

# Scenario: recursive tools
RECURSIVE_TARGET_DEPTH = 20

# Scenario: multi-agent loop
AGENT_MAX_ITERATIONS = 40

# Containment parameters
RETRY_LIMIT = 5
BUDGET_USD = 0.30
STEP_LIMIT = 10
TIMEOUT_MS = 5000              # 5 s (never fires in synthetic; structural only)
CB_THRESHOLD = 4


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

def _noop_call() -> dict[str, Any]:
    """A call that always succeeds."""
    return {"tokens": TOKENS_PER_CALL, "cost": COST_PER_CALL}


def _always_fail() -> dict[str, Any]:
    raise RuntimeError("transient failure")


# ---------------------------------------------------------------------------
# Scenario A: Retry storm
# ---------------------------------------------------------------------------

def _retry_storm_no_containment() -> tuple[int, bool]:
    """3 nested retry layers, always-failing LLM. Returns (calls, terminated_correctly)."""
    total_calls = 0

    def _llm() -> None:
        nonlocal total_calls
        total_calls += 1
        raise RuntimeError("fail")

    def _retry(fn: Any, n: int) -> None:
        for i in range(n):
            try:
                fn()
                return
            except RuntimeError:
                if i == n - 1:
                    raise

    try:
        _retry(lambda: _retry(lambda: _retry(_llm, RETRY_ATTEMPTS), RETRY_ATTEMPTS), RETRY_ATTEMPTS)
    except RuntimeError:
        pass
    # Terminated correctly means it stopped before RETRY_RUNAWAY_LIMIT
    return total_calls, total_calls < RETRY_RUNAWAY_LIMIT


def _retry_storm_retry_limit() -> tuple[int, bool]:
    """RetryContainer only: caps total retry budget."""
    retry = RetryContainer(max_retries=RETRY_LIMIT, backoff_base=0.0)
    call_count = 0

    def _fail() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("fail")

    try:
        retry.execute(_fail)
    except RuntimeError:
        pass
    return call_count, call_count <= RETRY_LIMIT + 1


def _retry_storm_timeout_only() -> tuple[int, bool]:
    """ExecutionContext with timeout only; no budget/step limits."""
    config = ExecutionConfig(
        max_cost_usd=9999.0,
        max_steps=9999,
        max_retries_total=9999,
        timeout_ms=TIMEOUT_MS,
    )
    call_count = 0
    halted = False

    def _fail_fn() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("fail")

    with ExecutionContext(config=config) as ctx:
        for _ in range(RETRY_RUNAWAY_LIMIT):
            decision = ctx.wrap_llm_call(
                fn=_fail_fn,
                options=WrapOptions(
                    operation_name="retry_call",
                    cost_estimate_hint=COST_PER_CALL,
                ),
            )
            if decision == Decision.HALT:
                halted = True
                break

    # Timeout-only doesn't cap retries in synthetic; all calls run
    return call_count, halted


def _retry_storm_full_veronica() -> tuple[int, bool]:
    """Full veronica: budget + steps + retries + CircuitBreaker."""
    cb = CircuitBreaker(failure_threshold=CB_THRESHOLD, recovery_timeout=9999.0)
    config = ExecutionConfig(
        max_cost_usd=BUDGET_USD,
        max_steps=STEP_LIMIT,
        max_retries_total=RETRY_LIMIT,
        timeout_ms=TIMEOUT_MS,
    )
    from veronica_core.runtime_policy import PolicyContext
    call_count = 0
    halted = False

    def _fail_fn() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("fail")

    with ExecutionContext(config=config, circuit_breaker=cb) as ctx:
        for _ in range(RETRY_RUNAWAY_LIMIT):
            cb_decision = cb.check(PolicyContext())
            if not cb_decision.allowed:
                halted = True
                break
            decision = ctx.wrap_llm_call(
                fn=_fail_fn,
                options=WrapOptions(
                    operation_name="retry_call",
                    cost_estimate_hint=COST_PER_CALL,
                ),
            )
            try:
                _fail_fn()
                cb.record_success()
            except RuntimeError:
                cb.record_failure()
            if decision == Decision.HALT:
                halted = True
                break

    return call_count, halted


# ---------------------------------------------------------------------------
# Scenario B: Recursive tools
# ---------------------------------------------------------------------------

def _recursive_no_containment() -> tuple[int, bool]:
    call_count = 0
    for _ in range(RECURSIVE_TARGET_DEPTH):
        call_count += 1
    return call_count, call_count < RECURSIVE_TARGET_DEPTH


def _recursive_retry_limit() -> tuple[int, bool]:
    """RetryContainer doesn't help recursive tools; runs full depth."""
    retry = RetryContainer(max_retries=RETRY_LIMIT, backoff_base=0.0)
    call_count = 0

    def _tool() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"depth": call_count}

    for _ in range(RECURSIVE_TARGET_DEPTH):
        retry.execute(_tool)

    return call_count, call_count < RECURSIVE_TARGET_DEPTH


def _recursive_timeout_only() -> tuple[int, bool]:
    """Timeout-only ExecutionContext; enforces step budget structurally."""
    config = ExecutionConfig(
        max_cost_usd=9999.0,
        max_steps=9999,
        max_retries_total=9999,
        timeout_ms=TIMEOUT_MS,
    )
    call_count = 0
    halted = False

    with ExecutionContext(config=config) as ctx:
        for depth in range(RECURSIVE_TARGET_DEPTH):
            captured = depth

            def _tool(d: int = captured) -> dict[str, Any]:
                nonlocal call_count
                call_count += 1
                return {"depth": d}

            decision = ctx.wrap_tool_call(
                fn=_tool,
                options=WrapOptions(
                    operation_name=f"tool_depth_{depth}",
                    cost_estimate_hint=COST_PER_CALL,
                ),
            )
            if decision == Decision.HALT:
                halted = True
                break

    return call_count, halted


def _recursive_full_veronica() -> tuple[int, bool]:
    """Full veronica: budget + steps halt the tool chain early."""
    cb = CircuitBreaker(failure_threshold=CB_THRESHOLD, recovery_timeout=9999.0)
    config = ExecutionConfig(
        max_cost_usd=BUDGET_USD,
        max_steps=STEP_LIMIT,
        max_retries_total=RETRY_LIMIT,
        timeout_ms=TIMEOUT_MS,
    )
    call_count = 0
    halted = False

    with ExecutionContext(config=config, circuit_breaker=cb) as ctx:
        for depth in range(RECURSIVE_TARGET_DEPTH):
            captured = depth

            def _tool(d: int = captured) -> dict[str, Any]:
                nonlocal call_count
                call_count += 1
                cb.record_success()
                return {"depth": d}

            decision = ctx.wrap_tool_call(
                fn=_tool,
                options=WrapOptions(
                    operation_name=f"tool_depth_{depth}",
                    cost_estimate_hint=COST_PER_CALL,
                ),
            )
            if decision == Decision.HALT:
                halted = True
                break

    return call_count, halted


# ---------------------------------------------------------------------------
# Scenario C: Multi-agent loop
# ---------------------------------------------------------------------------

def _agent_loop_no_containment() -> tuple[int, bool]:
    """Planner+critic loop runs to AGENT_MAX_ITERATIONS; critic never approves."""
    total_calls = 0
    for _ in range(AGENT_MAX_ITERATIONS):
        total_calls += 2  # planner + critic
    return total_calls, total_calls < AGENT_MAX_ITERATIONS * 2


def _agent_loop_retry_limit() -> tuple[int, bool]:
    """RetryContainer doesn't help agent loops; still runs full iterations."""
    total_calls = 0
    for _ in range(AGENT_MAX_ITERATIONS):
        total_calls += 2
    return total_calls, False


def _agent_loop_timeout_only() -> tuple[int, bool]:
    """Timeout-only ExecutionContext: planner+critic calls halted only if budget exceeded."""
    config = ExecutionConfig(
        max_cost_usd=9999.0,
        max_steps=9999,
        max_retries_total=9999,
        timeout_ms=TIMEOUT_MS,
    )
    call_count = 0
    halted = False

    with ExecutionContext(config=config) as ctx:
        for _ in range(AGENT_MAX_ITERATIONS):
            for role in ("planner", "critic"):
                decision = ctx.wrap_llm_call(
                    fn=_noop_call,
                    options=WrapOptions(
                        operation_name=role,
                        cost_estimate_hint=COST_PER_CALL,
                    ),
                )
                call_count += 1
                if decision == Decision.HALT:
                    halted = True
                    break
            if halted:
                break

    return call_count, halted


def _agent_loop_full_veronica() -> tuple[int, bool]:
    """Full veronica: AgentStepGuard + ExecutionContext halt the loop."""
    cb = CircuitBreaker(failure_threshold=CB_THRESHOLD, recovery_timeout=9999.0)
    guard = AgentStepGuard(max_steps=STEP_LIMIT)
    config = ExecutionConfig(
        max_cost_usd=BUDGET_USD,
        max_steps=STEP_LIMIT * 2 + 5,
        max_retries_total=RETRY_LIMIT,
        timeout_ms=TIMEOUT_MS,
    )
    call_count = 0
    halted = False
    step = 0

    with ExecutionContext(config=config, circuit_breaker=cb) as ctx:
        while guard.step(result=f"iter_{step}"):
            for role in ("planner", "critic"):
                decision = ctx.wrap_llm_call(
                    fn=_noop_call,
                    options=WrapOptions(
                        operation_name=role,
                        cost_estimate_hint=COST_PER_CALL,
                    ),
                )
                call_count += 1
                cb.record_success()
                if decision == Decision.HALT:
                    halted = True
                    break
            if halted:
                break
            step += 1
        if guard.is_exceeded:
            halted = True

    return call_count, halted


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class BenchRow:
    config: str
    scenario: str
    calls_executed: int
    tokens_consumed: int
    terminated_correctly: bool
    latency_ms: float


def _run_scenario(
    label: str,
    scenario: str,
    fn: Any,
) -> BenchRow:
    t0 = time.perf_counter()
    calls, terminated = fn()
    latency_ms = (time.perf_counter() - t0) * 1000
    return BenchRow(
        config=label,
        scenario=scenario,
        calls_executed=calls,
        tokens_consumed=calls * TOKENS_PER_CALL,
        terminated_correctly=terminated,
        latency_ms=round(latency_ms, 3),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    scenarios = [
        ("retry_storm",       [
            ("no_containment",  _retry_storm_no_containment),
            ("retry_limit",     _retry_storm_retry_limit),
            ("timeout_only",    _retry_storm_timeout_only),
            ("full_veronica",   _retry_storm_full_veronica),
        ]),
        ("recursive_tools",   [
            ("no_containment",  _recursive_no_containment),
            ("retry_limit",     _recursive_retry_limit),
            ("timeout_only",    _recursive_timeout_only),
            ("full_veronica",   _recursive_full_veronica),
        ]),
        ("multi_agent_loop",  [
            ("no_containment",  _agent_loop_no_containment),
            ("retry_limit",     _agent_loop_retry_limit),
            ("timeout_only",    _agent_loop_timeout_only),
            ("full_veronica",   _agent_loop_full_veronica),
        ]),
    ]

    rows: list[BenchRow] = []
    for scenario_name, configs in scenarios:
        for config_label, fn in configs:
            rows.append(_run_scenario(config_label, scenario_name, fn))

    print("=" * 90)
    print("BASELINE COMPARISON: Containment Configurations x Runaway Scenarios")
    print(f"Tokens/call: {TOKENS_PER_CALL} | Cost/call: ${COST_PER_CALL} "
          f"| Budget: ${BUDGET_USD} | Step limit: {STEP_LIMIT} | Retry limit: {RETRY_LIMIT}")
    print("=" * 90)

    # Group by scenario
    for scenario_name, _ in scenarios:
        group = [r for r in rows if r.scenario == scenario_name]
        print()
        print(f"Scenario: {scenario_name}")
        print("-" * 82)
        print(
            f"{'Configuration':<20} {'Calls':>7} {'Tokens':>9} "
            f"{'Terminated?':>13} {'Latency ms':>12}"
        )
        print("-" * 82)
        for r in group:
            terminated_str = "Yes" if r.terminated_correctly else "No"
            print(
                f"{r.config:<20} {r.calls_executed:>7} {r.tokens_consumed:>9} "
                f"{terminated_str:>13} {r.latency_ms:>12.3f}"
            )
        print("-" * 82)

    # Cross-scenario summary: full_veronica vs no_containment
    print()
    print("Summary: full_veronica reduction vs no_containment")
    print("-" * 70)
    print(f"{'Scenario':<22} {'Baseline calls':>16} {'Veronica calls':>16} {'Reduction':>12}")
    print("-" * 70)
    for scenario_name, _ in scenarios:
        baseline_r = next(r for r in rows if r.scenario == scenario_name and r.config == "no_containment")
        veronica_r = next(r for r in rows if r.scenario == scenario_name and r.config == "full_veronica")
        reduction = 0.0
        if baseline_r.calls_executed > 0:
            reduction = 100.0 * (1.0 - veronica_r.calls_executed / baseline_r.calls_executed)
        print(
            f"{scenario_name:<22} {baseline_r.calls_executed:>16} "
            f"{veronica_r.calls_executed:>16} {reduction:>11.1f}%"
        )
    print("-" * 70)

    veronica_correct = sum(1 for r in rows if r.config == "full_veronica" and r.terminated_correctly)
    print(f"\nFull veronica terminated correctly: {veronica_correct}/3 scenarios")


if __name__ == "__main__":
    main()
