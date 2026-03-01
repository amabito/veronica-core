"""Requirement-driven tests in EARS/Gherkin style (H-2).

Each test has a docstring stating the requirement:
  "REQUIREMENT: When [condition], the system shall [action]."
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Budget halt before network call
# ---------------------------------------------------------------------------


def test_budget_halt_blocks_fn_before_dispatch():
    """REQUIREMENT: When budget is exhausted, the system shall halt before calling fn()."""
    # Given: a context whose cost ceiling is already reached
    config = ExecutionConfig(max_cost_usd=0.10, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config)
    # Exhaust budget via a successful LLM call with cost hint
    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.10))

    # When: another call is attempted
    network_called = []
    decision = ctx.wrap_llm_call(fn=lambda: network_called.append(1))

    # Then: fn is not called and Decision.HALT is returned
    assert decision == Decision.HALT
    assert network_called == [], "fn must NOT be invoked when budget is exhausted"


def test_budget_halt_emits_budget_exceeded_event():
    """REQUIREMENT: When budget ceiling is reached, the system shall emit a CHAIN_BUDGET_EXCEEDED event."""
    # Given: a context with a cost estimate that would exceed the ceiling
    config = ExecutionConfig(max_cost_usd=0.05, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config)

    # When: a call with cost hint exceeding remaining budget is made
    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.10))

    # Then: a CHAIN_BUDGET_EXCEEDED event is recorded
    snap = ctx.get_snapshot()
    event_types = [e.event_type for e in snap.events]
    assert any("BUDGET" in t for t in event_types), (
        f"Expected a budget event, got: {event_types}"
    )


# ---------------------------------------------------------------------------
# Multi-agent cost propagation
# ---------------------------------------------------------------------------


def test_child_cost_propagates_to_parent():
    """REQUIREMENT: When a child context incurs cost, the system shall propagate that cost to the parent chain."""
    # Given: a parent context with sufficient budget and a spawned child
    config = ExecutionConfig(max_cost_usd=1.0, max_steps=20, max_retries_total=5)
    parent = ExecutionContext(config=config)
    child = parent.spawn_child(max_cost_usd=0.5)

    # When: the child incurs cost
    child.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.30))

    # Then: the parent accumulates the child's cost
    snap = parent.get_snapshot()
    assert snap.cost_usd_accumulated >= 0.30, (
        f"Parent cost should include child cost, got {snap.cost_usd_accumulated}"
    )


def test_child_budget_exhaustion_halts_parent():
    """REQUIREMENT: When a child's cost pushes the parent over its ceiling, the system shall halt parent calls."""
    # Given: a parent context with a tight ceiling
    config = ExecutionConfig(max_cost_usd=0.20, max_steps=20, max_retries_total=5)
    parent = ExecutionContext(config=config)
    child = parent.spawn_child(max_cost_usd=0.20)

    # When: the child spends the full shared budget
    child.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.20))

    # Then: the parent cannot make further calls
    decision = parent.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.01))
    assert decision == Decision.HALT


# ---------------------------------------------------------------------------
# Abort prevents future calls
# ---------------------------------------------------------------------------


def test_abort_prevents_subsequent_wrap_calls():
    """REQUIREMENT: When abort() is called, the system shall prevent all subsequent wrap calls from executing fn."""
    # Given: a running context
    config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
    ctx = ExecutionContext(config=config)

    # When: abort is called
    ctx.abort("user cancelled")

    # Then: future wrap calls return HALT without calling fn
    fn_called = []
    decision = ctx.wrap_llm_call(fn=lambda: fn_called.append(1))
    assert decision == Decision.HALT
    assert fn_called == []


# ---------------------------------------------------------------------------
# Degradation ladder triggers
# ---------------------------------------------------------------------------


def test_degradation_ladder_triggers_rate_limit_at_90pct():
    """REQUIREMENT: When cost reaches 90% of the ceiling, the system shall apply rate limiting."""
    from veronica_core.shield.degradation import DegradationConfig, DegradationLadder

    # Given: a ladder with default thresholds
    ladder = DegradationLadder(DegradationConfig())

    # When: cost fraction is at 91%
    decision = ladder.evaluate(cost_accumulated=0.91, max_cost_usd=1.0, current_model="gpt-4o")

    # Then: a RATE_LIMIT action is returned
    assert decision is not None
    assert decision.degradation_action == "RATE_LIMIT" or decision.policy_type == "rate_limit"


def test_degradation_ladder_triggers_model_downgrade_at_80pct():
    """REQUIREMENT: When cost reaches 80% of the ceiling, the system shall downgrade the model."""
    from veronica_core.shield.degradation import DegradationConfig, DegradationLadder

    # Given: a ladder with a model map configured
    ladder = DegradationLadder(
        DegradationConfig(model_map={"gpt-4o": "gpt-4o-mini"})
    )

    # When: cost fraction is at 82%
    decision = ladder.evaluate(cost_accumulated=0.82, max_cost_usd=1.0, current_model="gpt-4o")

    # Then: a MODEL_DOWNGRADE action is returned
    assert decision is not None
    assert decision.degradation_action == "MODEL_DOWNGRADE"
