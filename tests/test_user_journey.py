"""User journey tests: end-to-end agent lifecycle scenarios (M-3, S-7)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Primary journey: budget exhaustion and context reset
# ---------------------------------------------------------------------------


def test_journey_agent_runs_exhausts_budget_and_new_context_allows_again():
    """Journey: agent runs within budget -> budget exhausted -> new context -> runs again.

    Given an agent with a limited budget,
    When the agent runs until budget is exhausted,
    Then a fresh ExecutionContext allows the agent to run again.
    """
    # GIVEN: agent context with small budget
    config = ExecutionConfig(max_cost_usd=0.20, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config)

    # WHEN: agent runs until budget is exhausted
    results = []
    for _ in range(3):
        decision = ctx.wrap_llm_call(
            fn=lambda: results.append("call"),
            options=WrapOptions(cost_estimate_hint=0.08),
        )
        if decision == Decision.HALT:
            break

    # THEN: some calls succeeded before halt
    assert len(results) >= 2, "Expected at least 2 successful calls before budget halt"

    # AND WHEN: a fresh context is created (reset)
    ctx2 = ExecutionContext(config=config)

    # THEN: the new context allows calls again
    fresh_result = []
    decision2 = ctx2.wrap_llm_call(
        fn=lambda: fresh_result.append("fresh"),
        options=WrapOptions(cost_estimate_hint=0.05),
    )
    assert decision2 == Decision.ALLOW
    assert fresh_result == ["fresh"], "Fresh context must allow agent calls"


def test_journey_abort_then_new_context_recovers():
    """Journey: agent aborted -> new context -> agent resumes.

    Given an agent context that was aborted (e.g., user cancellation),
    When a new context is spawned,
    Then the new context operates without the abort state.
    """
    # GIVEN: a context that gets aborted
    config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
    ctx_aborted = ExecutionContext(config=config)
    ctx_aborted.abort("simulated failure")

    # Verify abort blocks calls
    assert ctx_aborted.wrap_llm_call(fn=lambda: None) == Decision.HALT

    # WHEN: new context is created
    ctx_fresh = ExecutionContext(config=config)

    # THEN: fresh context allows calls
    called = []
    decision = ctx_fresh.wrap_llm_call(fn=lambda: called.append(1))
    assert decision == Decision.ALLOW
    assert called == [1]


# ---------------------------------------------------------------------------
# Primary journey: multi-step with step limit
# ---------------------------------------------------------------------------


def test_journey_step_limit_enforced_across_multiple_llm_calls():
    """Journey: agent runs N steps -> step limit hit -> HALT.

    Given an agent with max_steps=3,
    When 3 calls succeed and a 4th is attempted,
    Then the 4th call returns HALT.
    """
    config = ExecutionConfig(max_cost_usd=100.0, max_steps=3, max_retries_total=10)
    ctx = ExecutionContext(config=config)

    decisions = []
    for _ in range(4):
        d = ctx.wrap_llm_call(fn=lambda: None)
        decisions.append(d)

    # First 3 should ALLOW, 4th should HALT
    assert decisions[:3] == [Decision.ALLOW, Decision.ALLOW, Decision.ALLOW]
    assert decisions[3] == Decision.HALT


# ---------------------------------------------------------------------------
# Secondary journey: child agent cost propagation
# ---------------------------------------------------------------------------


def test_journey_parent_child_budget_sharing():
    """Journey: parent spawns child agent -> child runs -> cost propagated to parent.

    Given a parent context with budget $1.00,
    When a child agent is spawned and makes LLM calls,
    Then the parent's accumulated cost reflects the child's spending.
    """
    # GIVEN: parent context
    config = ExecutionConfig(max_cost_usd=1.0, max_steps=50, max_retries_total=10)
    parent = ExecutionContext(config=config)

    # WHEN: child agent runs
    child = parent.spawn_child(max_cost_usd=0.50)
    child.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.25))
    child.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.20))

    # THEN: parent sees cost propagated from child
    snap = parent.get_snapshot()
    assert snap.cost_usd_accumulated >= 0.45, (
        f"Parent should have at least $0.45 accumulated, got ${snap.cost_usd_accumulated:.4f}"
    )


def test_journey_child_cost_propagation_halts_parent_on_third_call():
    """Journey: child agent cost propagates up; parent ceiling is respected on parent calls.

    Given a parent with $0.20 budget and a child that spends $0.20,
    When the child's cost propagates to the parent (putting parent at ceiling),
    Then the parent's next call returns HALT (budget exhausted).

    Note: The containment layer checks cost_estimate_hint against the child's own ceiling
    (not the parent's) pre-flight. Cost propagation to the parent happens post-success.
    Parent calls are blocked once parent cost_usd_accumulated >= parent ceiling.
    """
    config = ExecutionConfig(max_cost_usd=0.20, max_steps=10, max_retries_total=5)
    parent = ExecutionContext(config=config)

    # Child gets same limit as parent
    child = parent.spawn_child(max_cost_usd=0.20)

    # Child spends $0.20 (exactly at ceiling)
    d1 = child.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.20))
    assert d1 == Decision.ALLOW

    # Parent now has cost_usd_accumulated == 0.20 (propagated from child)
    snap = parent.get_snapshot()
    assert snap.cost_usd_accumulated >= 0.20

    # Parent's next call is halted (cost already at ceiling)
    d2 = parent.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.01))
    assert d2 == Decision.HALT


# ---------------------------------------------------------------------------
# Secondary journey: pipeline hook intervenes gracefully
# ---------------------------------------------------------------------------


def test_journey_pipeline_hook_blocks_and_caller_handles_halt():
    """Journey: pipeline hook blocks -> caller sees HALT -> continues safely.

    Given a ShieldPipeline with a hook that always blocks,
    When the agent makes an LLM call,
    Then the decision is HALT and the caller can handle it gracefully.
    """
    class BlockAllHook:
        def before_llm_call(self, ctx) -> Decision:
            return Decision.HALT

    pipeline = ShieldPipeline(pre_dispatch=BlockAllHook())
    config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
    ctx = ExecutionContext(config=config, pipeline=pipeline)

    fn_called = []
    decision = ctx.wrap_llm_call(fn=lambda: fn_called.append(1))

    assert decision == Decision.HALT
    assert fn_called == [], "fn must not execute when pipeline blocks"
