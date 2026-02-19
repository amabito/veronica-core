"""Smoke test: verify the Quickstart code from README runs without error.

This exercises the same pattern shown in the README Quickstart section
to ensure documented examples do not break silently.

No external API required. Runs in < 1 second.
"""

from __future__ import annotations

from veronica_core import (
    AdaptiveBudgetHook,
    BudgetWindowHook,
    ShieldConfig,
    TimeAwarePolicy,
)
from veronica_core.shield import Decision, ShieldPipeline, ToolCallContext


def test_quickstart_all_features_smoke():
    """README Quickstart pattern: all features enabled, 6 calls, no crash."""
    config = ShieldConfig()
    config.budget_window.enabled = True
    config.budget_window.max_calls = 5
    config.budget_window.window_seconds = 60.0
    config.token_budget.enabled = True
    config.token_budget.max_output_tokens = 500
    config.input_compression.enabled = True
    config.adaptive_budget.enabled = True
    config.time_aware_policy.enabled = True

    budget_hook = BudgetWindowHook(
        max_calls=config.budget_window.max_calls,
        window_seconds=config.budget_window.window_seconds,
    )
    adaptive = AdaptiveBudgetHook(base_ceiling=config.budget_window.max_calls)
    time_policy = TimeAwarePolicy()
    pipe = ShieldPipeline(pre_dispatch=budget_hook)

    decisions = []
    for i in range(6):
        ctx = ToolCallContext(request_id=f"call-{i+1}", tool_name="llm")
        decision = pipe.before_llm_call(ctx)
        decisions.append(decision)
        if decision == Decision.HALT:
            break

    # Calls 1-4: ALLOW, call 5: DEGRADE, call 6: HALT
    assert decisions[:4] == [Decision.ALLOW] * 4
    assert decisions[4] == Decision.DEGRADE
    assert decisions[5] == Decision.HALT

    # Safety events generated
    events = pipe.get_events()
    assert len(events) == 2
    assert events[0].event_type == "BUDGET_WINDOW_EXCEEDED"
    assert events[0].decision == Decision.DEGRADE
    assert events[1].event_type == "BUDGET_WINDOW_EXCEEDED"
    assert events[1].decision == Decision.HALT

    # Feed events into adaptive hook
    for ev in events:
        adaptive.feed_event(ev)
    result = adaptive.adjust()
    assert result.action == "hold"  # 2 events < tighten_trigger (3)
    assert result.adjusted_ceiling == 5

    # Export control state
    time_result = time_policy.evaluate(ctx)
    state = adaptive.export_control_state(
        time_multiplier=time_result.multiplier,
    )
    assert "adjusted_ceiling" in state
    assert "effective_multiplier" in state
    assert state["base_ceiling"] == 5
    assert 0 < state["effective_multiplier"] <= 1.2
