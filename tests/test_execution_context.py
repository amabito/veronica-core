"""Unit tests for the 3 wired TODO paths in ExecutionContext.

Tests:
1. CircuitBreaker OPEN must halt before fn() is ever called.
2. pipeline.before_charge() called exactly once per successful LLM call.
3. kind='tool' routes to before_tool_call(), not before_llm_call().
4. before_charge() must NOT be called for tool calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.types import Decision


def test_circuit_breaker_blocks_before_dispatch():
    """CircuitBreaker OPEN must halt before fn() is ever called."""
    from veronica_core.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=9999.0)
    breaker.record_failure()  # circuit OPEN

    config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config, circuit_breaker=breaker)

    called = []
    decision = ctx.wrap_llm_call(fn=lambda: called.append(1))
    assert decision == Decision.HALT
    assert called == [], "fn must not be called when circuit is OPEN"

    snap = ctx.get_snapshot()
    assert any(e.event_type == "CHAIN_CIRCUIT_OPEN" for e in snap.events)


def test_before_charge_called_once_per_llm_call():
    """pipeline.before_charge() called exactly once per successful LLM call."""

    class ChargeCapture:
        def __init__(self):
            self.calls: list[float] = []

        def before_charge(self, ctx, cost_usd: float):
            self.calls.append(cost_usd)
            return None  # ALLOW

    capture = ChargeCapture()
    pipeline = ShieldPipeline(budget=capture)

    config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config, pipeline=pipeline)

    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.05))
    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.10))

    assert len(capture.calls) == 2
    assert abs(capture.calls[0] - 0.05) < 1e-9
    assert abs(capture.calls[1] - 0.10) < 1e-9


def test_tool_routing_uses_before_tool_call():
    """kind='tool' must invoke before_tool_call(), not before_llm_call()."""

    class ToolHookSpy:
        def __init__(self):
            self.tool_calls = 0
            self.llm_calls = 0

        def before_tool_call(self, ctx) -> Decision | None:
            self.tool_calls += 1
            return None

        def before_llm_call(self, ctx) -> Decision | None:
            self.llm_calls += 1
            return None

    spy = ToolHookSpy()
    # ShieldPipeline accepts both pre_dispatch (LLM) and tool_dispatch (tool)
    pipeline = ShieldPipeline(pre_dispatch=spy, tool_dispatch=spy)

    config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config, pipeline=pipeline)

    ctx.wrap_tool_call(fn=lambda: None, options=WrapOptions(operation_name="my_tool"))
    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(operation_name="my_llm"))

    assert spy.tool_calls == 1, f"Expected 1 tool call, got {spy.tool_calls}"
    assert spy.llm_calls == 1, f"Expected 1 llm call, got {spy.llm_calls}"


def test_before_charge_skipped_for_tool_calls():
    """before_charge() must NOT be called for tool calls."""

    class ChargeCapture:
        def __init__(self):
            self.calls = 0

        def before_charge(self, ctx, cost_usd):
            self.calls += 1
            return None

    capture = ChargeCapture()
    pipeline = ShieldPipeline(budget=capture)
    config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config, pipeline=pipeline)

    ctx.wrap_tool_call(
        fn=lambda: None,
        options=WrapOptions(operation_name="tool", cost_estimate_hint=0.05),
    )
    assert capture.calls == 0, "before_charge must not fire for tool calls"
