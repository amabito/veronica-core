"""VERONICA runaway loop demo -- budget enforcement stops infinite retries.

Run:
    pip install -e .
    python examples/runaway_loop_demo.py
"""
from veronica_core import ExecutionContext, ExecutionConfig, WrapOptions


def main() -> None:
    # Hard limit: $0.05 total budget
    config = ExecutionConfig(
        max_cost_usd=0.05,
        max_steps=1000,
        max_retries_total=1000,
        timeout_ms=0,
    )

    print("Starting runaway loop...")
    print("Each call costs $0.01. Budget limit: $0.05.")
    print()

    call_count = 0
    with ExecutionContext(config=config) as ctx:
        while True:
            decision = ctx.wrap_llm_call(
                fn=lambda: "simulated response",
                options=WrapOptions(
                    operation_name=f"retry_{call_count}",
                    cost_estimate_hint=0.01,
                ),
            )
            call_count += 1
            print(f"  Call {call_count}: $0.01 -> {decision.name}")

            if decision.name == "HALT":
                break

    snap = ctx.get_graph_snapshot()
    total = snap["aggregates"]["total_cost_usd"]
    print()
    print(f"HALTED after {call_count} calls. Total cost: ${total:.2f}")
    print()
    print("Without VERONICA: infinite retries, $12,000 bill.")
    print("With VERONICA: hard stop, zero damage.")


if __name__ == "__main__":
    main()
