"""Budget + Degrade demo -- runs in < 1 second, no external API needed.

Shows three behaviors:
  1. ALLOW zone  -- calls proceed normally
  2. DEGRADE zone -- budget threshold crossed, model fallback signaled
  3. HALT         -- budget ceiling hit, execution stopped

Usage:
    pip install -e .
    python examples/budget_degrade_demo.py
"""

from __future__ import annotations

from veronica_core.shield import (
    BudgetWindowHook,
    Decision,
    ShieldPipeline,
    ToolCallContext,
)

DEGRADE_MAP = {"gpt-4": "gpt-3.5-turbo"}


def main() -> None:
    # 5 calls allowed, DEGRADE at 80% (call 5), HALT at call 6
    hook = BudgetWindowHook(max_calls=5, window_seconds=60.0, degrade_threshold=0.8)
    pipe = ShieldPipeline(pre_dispatch=hook)

    model = "gpt-4"

    for i in range(1, 8):
        ctx = ToolCallContext(request_id=f"call-{i}", tool_name="llm", model=model)
        decision = pipe.before_llm_call(ctx)

        if decision is Decision.DEGRADE:
            # Apply model fallback
            model = DEGRADE_MAP.get(model, model)
            print(f"Call {i:2d} / model={ctx.model:<16s} -> DEGRADE (fallback to {model})")
        elif decision is Decision.HALT:
            print(f"Call {i:2d} / model={model:<16s} -> HALT")
            break
        else:
            print(f"Call {i:2d} / model={model:<16s} -> ALLOW")

    # Print safety events (structured evidence)
    print()
    for event in pipe.get_events():
        print(f"SafetyEvent: {event.event_type} / {event.decision.value:<8s} / {event.hook}")


if __name__ == "__main__":
    main()
