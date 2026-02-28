"""VERONICA quickstart demo -- 2-line budget enforcement.

Competes with AgentBudget's agentbudget.init("$5.00") by offering
the same simplicity with veronica-core's full safety stack underneath.

Demonstrates:
  1. The 2-line quickstart (AgentBudget-equivalent DX)
  2. Custom parameters (max_steps, max_retries)
  3. Manual wrapping with get_context()
  4. Progressive disclosure: 2-line -> full API graduation

No real LLM calls -- uses simulated responses.

Run:
    pip install -e .
    python examples/quickstart_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from repo root without installing.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import veronica_core
from veronica_core.containment import (
    ChainMetadata,
    ExecutionConfig,
    ExecutionContext,
    WrapOptions,
)
from veronica_core.shield.types import Decision

SEPARATOR = "-" * 60


# ---------------------------------------------------------------------------
# Level 1: 2-line quickstart
# ---------------------------------------------------------------------------


def demo_level1_two_line() -> None:
    print(SEPARATOR)
    print("LEVEL 1 -- 2-line quickstart (AgentBudget-equivalent DX)")
    print(SEPARATOR)
    print()
    print("  Code:")
    print('    ctx = veronica_core.init("$0.10")')
    print("    veronica_core.shutdown()")
    print()

    ctx = veronica_core.init("$0.10")

    # Simulate a few LLM calls using the returned context.
    for i in range(3):
        decision = ctx.wrap_llm_call(
            fn=lambda: f"simulated response {i}",
            options=WrapOptions(
                operation_name=f"llm_call_{i}",
                cost_estimate_hint=0.02,
            ),
        )
        snap = ctx.get_snapshot()
        print(
            f"  Call {i + 1}: {decision.name:<5}"
            f"  spent=${snap.cost_usd_accumulated:.4f}"
        )

    snap = ctx.get_snapshot()
    print()
    print("  Budget ceiling:  $0.10")
    print(f"  Total spent:     ${snap.cost_usd_accumulated:.4f}")
    print(f"  Steps taken:     {snap.step_count}")
    print(f"  Aborted:         {snap.aborted}")

    veronica_core.shutdown()

    print()
    print("  [PASS] 2-line quickstart: init() + shutdown().")
    print()


# ---------------------------------------------------------------------------
# Level 2: Custom parameters
# ---------------------------------------------------------------------------


def demo_level2_custom_params() -> None:
    print(SEPARATOR)
    print("LEVEL 2 -- Custom parameters (max_steps, max_retries)")
    print(SEPARATOR)
    print()
    print("  Code:")
    print('    ctx = veronica_core.init(')
    print('        "$0.50",')
    print("        max_steps=5,")
    print("        max_retries_total=10,")
    print("    )")
    print()

    ctx = veronica_core.init(
        "$0.50",
        max_steps=5,
        max_retries_total=10,
    )

    halt_at: int | None = None
    for i in range(10):
        decision = ctx.wrap_llm_call(
            fn=lambda: f"step {i}",
            options=WrapOptions(operation_name=f"agent_step_{i}"),
        )
        print(f"  Step {i + 1}: {decision.name}")
        if decision == Decision.HALT:
            halt_at = i + 1
            break

    snap = ctx.get_snapshot()
    print()
    print(f"  max_steps=5, halted at iteration: {halt_at}")
    print(f"  Step count: {snap.step_count}")

    veronica_core.shutdown()

    assert snap.step_count == 5, f"Expected 5 steps, got {snap.step_count}"
    assert halt_at == 6, f"Expected halt at loop index 6, got {halt_at}"

    print()
    print("  [PASS] Step limit enforced at iteration 6 (step_count=5).")
    print()


# ---------------------------------------------------------------------------
# Level 3: Manual wrapping with get_context()
# ---------------------------------------------------------------------------


def demo_level3_get_context() -> None:
    print(SEPARATOR)
    print("LEVEL 3 -- Manual wrapping with get_context()")
    print(SEPARATOR)
    print()
    print("  Useful when init() is called in one module and LLM calls")
    print("  are made in another (service-oriented architecture).")
    print()
    print("  Code:")
    print('    veronica_core.init("$1.00")')
    print()
    print("    # In another module:")
    print("    ctx = veronica_core.get_context()")
    print("    if ctx is not None:")
    print("        decision = ctx.wrap_llm_call(fn=my_llm_fn, ...)")
    print()

    veronica_core.init("$1.00")

    # Simulate another module calling get_context()
    ctx = veronica_core.get_context()
    assert ctx is not None, "get_context() must return context after init()"

    decision = ctx.wrap_llm_call(
        fn=lambda: "answer from another module",
        options=WrapOptions(
            operation_name="cross_module_call",
            cost_estimate_hint=0.03,
        ),
    )
    snap = ctx.get_snapshot()
    print(f"  Cross-module call decision: {decision.name}")
    print(f"  Spent:  ${snap.cost_usd_accumulated:.4f}")
    print(f"  Steps:  {snap.step_count}")

    veronica_core.shutdown()

    # After shutdown, get_context() returns None.
    assert veronica_core.get_context() is None

    print()
    print("  [PASS] get_context() works across module boundaries.")
    print()


# ---------------------------------------------------------------------------
# Level 4: Full API -- ExecutionContext directly (graduation path)
# ---------------------------------------------------------------------------


def demo_level4_full_api() -> None:
    print(SEPARATOR)
    print("LEVEL 4 -- Full API (ExecutionContext + ChainMetadata)")
    print(SEPARATOR)
    print()
    print("  For production use cases requiring audit trails, per-chain")
    print("  metadata, and structured event inspection.")
    print()

    config = ExecutionConfig(
        max_cost_usd=0.20,
        max_steps=10,
        max_retries_total=20,
        timeout_ms=0,
    )
    meta = ChainMetadata(
        request_id="req-demo-001",
        chain_id="chain-demo-001",
        org_id="acme-corp",
        team="ml-platform",
        service="summariser",
        model="gpt-4o",
        tags={"env": "demo"},
    )

    with ExecutionContext(config=config, metadata=meta) as ctx:
        for i in range(4):
            decision = ctx.wrap_llm_call(
                fn=lambda: f"full api response {i}",
                options=WrapOptions(
                    operation_name=f"summarise_{i}",
                    cost_estimate_hint=0.04,
                ),
            )
            snap = ctx.get_snapshot()
            print(
                f"  Call {i + 1}: {decision.name:<5}"
                f"  spent=${snap.cost_usd_accumulated:.4f}"
                f"  steps={snap.step_count}"
            )
            if decision == Decision.HALT:
                break

    snap = ctx.get_snapshot()
    print()
    print(f"  Final cost:   ${snap.cost_usd_accumulated:.4f}")
    print(f"  Final steps:  {snap.step_count}")
    print(f"  Events:       {len(snap.events)}")
    if snap.events:
        for ev in snap.events:
            print(f"    [{ev.decision}] {ev.event_type} -- {ev.reason}")

    print()
    print("  [PASS] Full ExecutionContext + ChainMetadata API.")
    print()


# ---------------------------------------------------------------------------
# Summary comparison table
# ---------------------------------------------------------------------------


def print_comparison_table() -> None:
    print("=" * 60)
    print("API COMPARISON: AgentBudget vs. veronica-core quickstart")
    print("=" * 60)
    print()

    rows = [
        ("Feature",                "AgentBudget",      "veronica-core"),
        ("-" * 24,                 "-" * 18,            "-" * 16),
        ("Init",                   'agentbudget.init()',  'veronica_core.init()'),
        ("Budget string",          '"$5.00"',           '"$5.00"'),
        ("Step limit",             "built-in",          "max_steps=1000"),
        ("Retry budget",           "N/A",               "max_retries_total=50"),
        ("SDK patching",           "auto",              "patch_openai=True"),
        ("Manual wrap",            "N/A",               "ctx.wrap_llm_call()"),
        ("Full audit trail",       "N/A",               "get_snapshot()"),
        ("Circuit breaker",        "N/A",               "CircuitBreaker"),
        ("Distributed budget",     "N/A",               "RedisBudgetBackend"),
        ("Compliance export",      "N/A",               "ComplianceExporter"),
        ("Safety event log",       "N/A",               "snap.events"),
        ("Execution graph",        "N/A",               "get_graph_snapshot()"),
        ("Lines for 2-line DX",    "2",                 "2"),
    ]

    col_widths = [26, 20, 18]
    for row in rows:
        line = "  ".join(
            str(cell).ljust(w) for cell, w in zip(row, col_widths)
        )
        print(f"  {line}")

    print()
    print("  veronica-core is a drop-in replacement for AgentBudget")
    print("  with the same 2-line DX and a full safety stack on top.")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    print()
    print("VERONICA Quickstart Demo")
    print("=" * 60)
    print()
    print("  veronica-core.init() is designed to be a drop-in upgrade")
    print("  from AgentBudget: same 2-line setup, full safety stack.")
    print()

    try:
        demo_level1_two_line()
        demo_level2_custom_params()
        demo_level3_get_context()
        demo_level4_full_api()
        print_comparison_table()

        print("=" * 60)
        print("ALL DEMO LEVELS PASSED")
        print("=" * 60)
        print()

    except AssertionError as exc:
        print(f"\n[FAIL] Assertion failed: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"\n[FAIL] Unexpected error: {exc}")
        raise


if __name__ == "__main__":
    main()
