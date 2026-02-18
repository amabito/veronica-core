"""Token Budget + MinimalResponsePolicy demo (v0.4.3).

Demonstrates:
- TokenBudgetHook: DEGRADE at 80% of output token ceiling, HALT at 100%
- MinimalResponsePolicy: injects conciseness constraints into system messages

Run:
    pip install -e .
    python examples/token_budget_minimal_demo.py

Expected output:
    --- TokenBudgetHook demo ---
    Tokens used:    0 / 100  -> ALLOW
    Tokens used:   70 / 100  -> ALLOW
    Tokens used:   80 / 100  -> DEGRADE  (80% threshold reached)
    Tokens used:   95 / 100  -> DEGRADE
    Tokens used:  100 / 100  -> HALT     (ceiling reached)
    SafetyEvent: TOKEN_BUDGET_EXCEEDED / DEGRADE / TokenBudgetHook
    SafetyEvent: TOKEN_BUDGET_EXCEEDED / HALT   / TokenBudgetHook

    --- MinimalResponsePolicy demo ---
    [disabled] system message unchanged: You are a helpful assistant.
    [enabled]  system message with constraints injected (see below):
    You are a helpful assistant.

    --- RESPONSE CONSTRAINTS (enforced by VERONICA MinimalResponsePolicy) ---
    - Answer in 1 line (conclusion first).
    - Use at most 3 bullet points if elaboration needed.
    - If uncertain, state 'uncertain' in 1 line + suggest 1 next action.
    - No follow-up questions.
    --- END CONSTRAINTS ---
"""

from __future__ import annotations

from veronica_core.shield import (
    Decision,
    ShieldPipeline,
    TokenBudgetHook,
    ToolCallContext,
)
from veronica_core.policies.minimal_response import MinimalResponsePolicy

CTX = ToolCallContext(request_id="demo", tool_name="llm")


def demo_token_budget() -> None:
    print("--- TokenBudgetHook demo ---")

    hook = TokenBudgetHook(max_output_tokens=100, degrade_threshold=0.8)
    pipe = ShieldPipeline(pre_dispatch=hook)

    steps = [
        (0, "initial check"),
        (70, "70 tokens used"),
        (10, "80 tokens total"),
        (15, "95 tokens total"),
        (5, "100 tokens total (ceiling)"),
    ]

    for add_tokens, label in steps:
        hook.record_usage(output_tokens=add_tokens)
        decision = pipe.before_llm_call(CTX)
        marker = ""
        if decision is Decision.DEGRADE:
            marker = "  (80% threshold reached)" if hook.output_total == 80 else ""
        elif decision is Decision.HALT:
            marker = "  (ceiling reached)"
        print(
            f"  Tokens used: {hook.output_total:4d} / 100  -> {decision.value}{marker}"
        )

    events = pipe.get_events()
    print()
    for ev in events:
        print(f"  SafetyEvent: {ev.event_type} / {ev.decision.value:<7} / {ev.hook}")


def demo_minimal_response() -> None:
    print("\n--- MinimalResponsePolicy demo ---")

    system_msg = "You are a helpful assistant."

    # Disabled (default)
    policy_off = MinimalResponsePolicy(enabled=False)
    result_off = policy_off.inject(system_msg)
    print(f"  [disabled] system message unchanged: {result_off}")

    # Enabled
    policy_on = MinimalResponsePolicy(enabled=True, max_bullets=3, allow_questions=False)
    result_on = policy_on.inject(system_msg)
    print(f"  [enabled]  system message with constraints injected (see below):")
    print(f"  {result_on}")


if __name__ == "__main__":
    demo_token_budget()
    demo_minimal_response()
