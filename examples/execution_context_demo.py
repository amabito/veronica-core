"""ExecutionContext demo - four self-contained scenarios.

No real LLM calls are made. All callables use lambda stubs.

Run with:
    python examples/execution_context_demo.py

or, from the repo root:
    python -m examples.execution_context_demo
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment import (
    ChainMetadata,
    ExecutionConfig,
    ExecutionContext,
    WrapOptions,
)
from veronica_core.shield.types import Decision

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEPARATOR = "-" * 60


def print_snapshot(ctx: ExecutionContext) -> None:
    snap = ctx.get_snapshot()
    print(f"  steps:   {snap.step_count}")
    print(f"  cost:    ${snap.cost_usd_accumulated:.4f}")
    print(f"  retries: {snap.retries_used}")
    print(f"  aborted: {snap.aborted} ({snap.abort_reason!r})")
    print(f"  elapsed: {snap.elapsed_ms:.1f} ms")
    if snap.events:
        print(f"  events:  {[e.event_type for e in snap.events]}")
    if snap.nodes:
        statuses = [f"{n.operation_name or n.kind}:{n.status}" for n in snap.nodes]
        print(f"  nodes:   {statuses}")


def print_graph_summary(ctx: ExecutionContext) -> None:
    """Print aggregate graph summary after a scenario."""
    snap = ctx.get_snapshot()
    g = snap.graph_summary or {}
    cost = g.get("total_cost_usd", 0.0)
    llm = g.get("total_llm_calls", 0)
    tool = g.get("total_tool_calls", 0)
    retries = g.get("total_retries", 0)
    depth = g.get("max_depth", 0)
    llm_per_root = g.get("llm_calls_per_root", 0.0)
    tool_per_root = g.get("tool_calls_per_root", 0.0)
    print(
        f"  Graph: cost=${cost:.4f}, llm={llm}, tool={tool}, retries={retries},"
        f" depth={depth}, llm/root={llm_per_root:.1f}, tool/root={tool_per_root:.1f}"
    )


def print_graph_nodes(ctx: ExecutionContext) -> None:
    """Print full node list from the execution graph."""
    graph_snap = ctx.get_graph_snapshot()
    nodes = graph_snap.get("nodes", {})
    print("  Nodes:")
    for node in nodes.values():
        kind = node["kind"]
        name = node["name"]
        status = node["status"]
        cost = node["cost_usd"]
        stop = node.get("stop_reason")
        node_id = node["node_id"]
        if stop:
            print(f"    {node_id}  {kind:<6}  {name:<25}  {status:<7}  stop={stop}")
        else:
            print(f"    {node_id}  {kind:<6}  {name:<25}  {status:<7}  cost=${cost:.4f}")


# ---------------------------------------------------------------------------
# Scenario 1 - Single request chain
# ---------------------------------------------------------------------------


def scenario_single_request() -> None:
    print(SEPARATOR)
    print("SCENARIO 1 - Single request chain")
    print(SEPARATOR)

    config = ExecutionConfig(
        max_cost_usd=1.00,
        max_steps=10,
        max_retries_total=3,
        timeout_ms=0,
    )
    meta = ChainMetadata(
        request_id="req-001",
        chain_id="chain-001",
        org_id="acme",
        team="ml-platform",
        service="summariser",
        user_id="user-42",
        model="gpt-4o",
        tags={"env": "demo"},
    )

    with ExecutionContext(config=config, metadata=meta) as ctx:
        decision = ctx.wrap_llm_call(
            fn=lambda: "fake LLM response",
            options=WrapOptions(
                operation_name="summarise",
                cost_estimate_hint=0.02,
            ),
        )
        print(f"  wrap_llm_call decision: {decision}")
        assert decision == Decision.ALLOW, f"Expected ALLOW, got {decision}"

    print("  Final snapshot:")
    print_snapshot(ctx)
    print_graph_summary(ctx)
    print("  [PASS] Single request completed cleanly.")
    print()


# ---------------------------------------------------------------------------
# Scenario 2 - Agent loop halted by step limit
# ---------------------------------------------------------------------------


def scenario_agent_loop_step_limit() -> None:
    print(SEPARATOR)
    print("SCENARIO 2 - Agent loop: 10 iterations, step_limit=5")
    print(SEPARATOR)

    config = ExecutionConfig(
        max_cost_usd=10.00,
        max_steps=5,
        max_retries_total=20,
        timeout_ms=0,
    )

    ctx = ExecutionContext(config=config)
    halt_at_step: int | None = None

    for i in range(10):
        decision = ctx.wrap_llm_call(
            fn=lambda: f"step {i} result",
            options=WrapOptions(operation_name=f"agent_step_{i}"),
        )
        print(f"  step {i}: {decision}")
        if decision == Decision.HALT:
            halt_at_step = i
            break

    snap = ctx.get_snapshot()
    print(f"\n  Loop halted at iteration {halt_at_step} (step_count={snap.step_count})")
    print("  Final snapshot:")
    print_snapshot(ctx)
    print_graph_summary(ctx)
    print_graph_nodes(ctx)
    assert snap.step_count == 5, f"Expected 5 steps, got {snap.step_count}"
    assert halt_at_step == 5, f"Expected halt at loop index 5, got {halt_at_step}"
    print("  [PASS] Step limit enforced at step 5.")
    print()


# ---------------------------------------------------------------------------
# Scenario 3 - Budget stop via cost_estimate_hint
# ---------------------------------------------------------------------------


def scenario_budget_stop() -> None:
    print(SEPARATOR)
    print("SCENARIO 3 - Budget stop: max_cost_usd=0.10, hint=0.04 per call")
    print(SEPARATOR)

    config = ExecutionConfig(
        max_cost_usd=0.10,
        max_steps=100,
        max_retries_total=10,
        timeout_ms=0,
    )

    ctx = ExecutionContext(config=config)
    call_count = 0

    # Each call has a cost_estimate_hint of 0.04.
    # Call 1: accumulated=0.00, hint=0.04, projected=0.04 < 0.10 -> ALLOW (accumulated becomes 0.04)
    # Call 2: accumulated=0.04, hint=0.04, projected=0.08 < 0.10 -> ALLOW (accumulated becomes 0.08)
    # Call 3: accumulated=0.08, hint=0.04, projected=0.12 > 0.10 -> HALT (pre-flight estimate check)
    for i in range(10):
        decision = ctx.wrap_llm_call(
            fn=lambda: "result",
            options=WrapOptions(
                operation_name=f"call_{i}",
                cost_estimate_hint=0.04,
            ),
        )
        print(f"  call {i}: {decision}")
        if decision == Decision.HALT:
            break
        call_count += 1

    snap = ctx.get_snapshot()
    print(f"\n  Calls completed: {call_count}, accumulated: ${snap.cost_usd_accumulated:.4f}")
    print("  Final snapshot:")
    print_snapshot(ctx)
    print_graph_summary(ctx)
    print_graph_nodes(ctx)
    assert call_count == 2, f"Expected 2 successful calls, got {call_count}"
    print("  [PASS] Budget ceiling enforced on 3rd call.")
    print()


# ---------------------------------------------------------------------------
# Scenario 4 - abort() cancels remaining work
# ---------------------------------------------------------------------------


def scenario_abort() -> None:
    print(SEPARATOR)
    print("SCENARIO 4 - abort() cancels remaining work")
    print(SEPARATOR)

    config = ExecutionConfig(
        max_cost_usd=100.00,
        max_steps=100,
        max_retries_total=50,
        timeout_ms=0,
    )

    ctx = ExecutionContext(config=config)

    # First call succeeds.
    d1 = ctx.wrap_llm_call(
        fn=lambda: "step 1",
        options=WrapOptions(operation_name="step_1"),
    )
    print(f"  call 1 (before abort): {d1}")
    assert d1 == Decision.ALLOW

    # Application code decides to cancel.
    print("  Calling ctx.abort('user cancelled') ...")
    ctx.abort("user cancelled")

    # All subsequent calls must HALT immediately.
    d2 = ctx.wrap_llm_call(
        fn=lambda: "step 2",
        options=WrapOptions(operation_name="step_2"),
    )
    print(f"  call 2 (after abort): {d2}")
    assert d2 == Decision.HALT

    d3 = ctx.wrap_tool_call(
        fn=lambda: "tool",
        options=WrapOptions(operation_name="tool_call"),
    )
    print(f"  tool call (after abort): {d3}")
    assert d3 == Decision.HALT

    snap = ctx.get_snapshot()
    print(f"\n  aborted={snap.aborted}, reason={snap.abort_reason!r}")
    print("  Final snapshot:")
    print_snapshot(ctx)
    print_graph_summary(ctx)
    print_graph_nodes(ctx)
    assert snap.aborted is True
    assert snap.abort_reason == "user cancelled"
    print("  [PASS] abort() blocked all subsequent calls.")
    print()


# ---------------------------------------------------------------------------
# Scenario 5 - Circuit breaker pre-opened halts execution
# ---------------------------------------------------------------------------


def scenario_circuit_open() -> None:
    print(SEPARATOR)
    print("SCENARIO 5 - CHAIN_CIRCUIT_OPEN: circuit breaker pre-opened")
    print(SEPARATOR)

    from veronica_core.circuit_breaker import CircuitBreaker

    config = ExecutionConfig(
        max_cost_usd=100.00,
        max_steps=100,
        max_retries_total=50,
        timeout_ms=0,
    )

    # Pre-open the circuit by recording failures up to threshold.
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=9999.0)
    breaker.record_failure()  # circuit now OPEN
    assert breaker.state.value == "OPEN"

    ctx = ExecutionContext(config=config, circuit_breaker=breaker)

    called = []
    decision = ctx.wrap_llm_call(
        fn=lambda: called.append("called"),
        options=WrapOptions(operation_name="should_not_run"),
    )

    print(f"  decision: {decision}")
    assert decision == Decision.HALT
    assert len(called) == 0, "fn() must not be called when circuit is OPEN"

    snap = ctx.get_snapshot()
    print(f"  events: {[e.event_type for e in snap.events]}")
    assert any(e.event_type == "CHAIN_CIRCUIT_OPEN" for e in snap.events)
    print_graph_summary(ctx)
    print_graph_nodes(ctx)
    print("  [PASS] CHAIN_CIRCUIT_OPEN halted execution before fn() dispatch.")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    print("ExecutionContext Demo")
    print("=" * 60)
    print()

    try:
        scenario_single_request()
        scenario_agent_loop_step_limit()
        scenario_budget_stop()
        scenario_abort()
        scenario_circuit_open()
        print("=" * 60)
        print("ALL SCENARIOS PASSED")
    except AssertionError as exc:
        print(f"\n[FAIL] Assertion failed: {exc}")
        sys.exit(1)
