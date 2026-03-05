"""Adversarial test: prompt loop attack.

Attack pattern: An agent is instructed to "call the tool again" repeatedly,
attempting to bypass step limits through instruction injection in prompts.

Tests verify that:
1. Without containment: attack succeeds (unlimited loops possible)
2. With veronica ExecutionContext + step limit: attack is contained
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Stub LLM that implements the "call again" attack pattern
# ---------------------------------------------------------------------------


class LoopingStubLLM:
    """Simulates an LLM that obeys "call the tool again" instructions.

    Each call increments a counter and returns a response asking to
    call again -- modeling a prompt injection loop attack.
    """

    def __init__(self) -> None:
        self.call_count = 0

    def call(self) -> str:
        self.call_count += 1
        # Simulate LLM returning "tool_call: call_again" instruction
        return f"Step {self.call_count}: calling tool again..."

    def run_unconstrained_loop(self, max_iterations: int = 1000) -> int:
        """Run the attack loop without any containment. Returns call count."""
        for _ in range(max_iterations):
            self.call()
        return self.call_count


# ---------------------------------------------------------------------------
# Without containment baseline tests
# ---------------------------------------------------------------------------


class TestPromptLoopWithoutContainment:
    """Baseline: confirm the attack succeeds without containment."""

    def test_unconstrained_loop_runs_unlimited(self) -> None:
        """Without containment, the loop runs the full requested iterations."""
        llm = LoopingStubLLM()
        total = llm.run_unconstrained_loop(max_iterations=50)
        # The attack succeeds: all 50 iterations complete
        assert total == 50

    def test_manual_loop_no_limit(self) -> None:
        """Without ExecutionContext, wrap_llm_call is never checked."""
        llm = LoopingStubLLM()
        # Simulated agent loop with no containment
        for _ in range(100):
            llm.call()
        assert llm.call_count == 100


# ---------------------------------------------------------------------------
# With containment: ExecutionContext stops the loop
# ---------------------------------------------------------------------------


class TestPromptLoopContained:
    """ExecutionContext must stop the loop at max_steps."""

    def test_step_limit_stops_attack(self) -> None:
        """Loop attack is halted when step limit is reached."""
        llm = LoopingStubLLM()
        # max_retries_total=5: budget for the chain (not per-call retries)
        # max_steps=5: only 5 LLM calls allowed
        config = ExecutionConfig(max_cost_usd=100.0, max_steps=5, max_retries_total=5)
        ctx = ExecutionContext(config=config)

        halt_count = 0
        allow_count = 0
        # Attempt 20 iterations (well over the 5-step limit)
        for _ in range(20):
            decision = ctx.wrap_llm_call(fn=llm.call)
            if decision == Decision.HALT:
                halt_count += 1
            elif decision == Decision.ALLOW:
                allow_count += 1

        # Exactly max_steps calls should complete
        assert allow_count == 5
        # All remaining are halted
        assert halt_count == 15
        # LLM was only called 5 times, not 20
        assert llm.call_count == 5

    def test_step_limit_one_stops_immediately(self) -> None:
        """With max_steps=1, second call is immediately blocked."""
        llm = LoopingStubLLM()
        config = ExecutionConfig(max_cost_usd=100.0, max_steps=1, max_retries_total=5)
        ctx = ExecutionContext(config=config)

        first = ctx.wrap_llm_call(fn=llm.call)
        second = ctx.wrap_llm_call(fn=llm.call)
        third = ctx.wrap_llm_call(fn=llm.call)

        assert first == Decision.ALLOW
        assert second == Decision.HALT
        assert third == Decision.HALT
        assert llm.call_count == 1

    def test_snapshot_records_halt_event(self) -> None:
        """ContextSnapshot records step_limit_exceeded event after halt."""
        llm = LoopingStubLLM()
        config = ExecutionConfig(max_cost_usd=100.0, max_steps=3, max_retries_total=5)
        ctx = ExecutionContext(config=config)

        for _ in range(5):
            ctx.wrap_llm_call(fn=llm.call)

        snap = ctx.get_snapshot()
        assert snap.step_count == 3
        event_types = {e.event_type for e in snap.events}
        assert "CHAIN_STEP_LIMIT_EXCEEDED" in event_types

    def test_cost_limit_also_stops_attack(self) -> None:
        """Budget ceiling halts loop even if step limit not reached."""
        llm = LoopingStubLLM()
        # 10 steps allowed but cost ceiling is $0.30 (3 calls @ $0.10 each)
        config = ExecutionConfig(max_cost_usd=0.30, max_steps=10, max_retries_total=5)
        ctx = ExecutionContext(config=config)

        results = []
        for _ in range(10):
            decision = ctx.wrap_llm_call(
                fn=llm.call,
                options=WrapOptions(cost_estimate_hint=0.10),
            )
            results.append(decision)

        allow_count = results.count(Decision.ALLOW)
        halt_count = results.count(Decision.HALT)

        # Only 3 calls fit within $0.30 budget
        assert allow_count == 3
        assert halt_count == 7
        assert llm.call_count == 3

    def test_abort_cancels_all_subsequent_calls(self) -> None:
        """Explicit abort() stops all subsequent wrap_llm_call invocations."""
        llm = LoopingStubLLM()
        config = ExecutionConfig(max_cost_usd=100.0, max_steps=100, max_retries_total=5)
        ctx = ExecutionContext(config=config)

        # Run 3 calls successfully
        for _ in range(3):
            ctx.wrap_llm_call(fn=llm.call)

        # Abort the context (e.g., user cancellation)
        ctx.abort("user_cancelled_attack")

        # All subsequent calls should be halted
        for _ in range(10):
            decision = ctx.wrap_llm_call(fn=llm.call)
            assert decision == Decision.HALT

        assert llm.call_count == 3

    def test_step_count_in_snapshot_matches_actual_calls(self) -> None:
        """step_count in snapshot must exactly match successful LLM calls."""
        llm = LoopingStubLLM()
        config = ExecutionConfig(max_cost_usd=100.0, max_steps=7, max_retries_total=5)
        ctx = ExecutionContext(config=config)

        for _ in range(20):
            ctx.wrap_llm_call(fn=llm.call)

        snap = ctx.get_snapshot()
        assert snap.step_count == 7
        assert llm.call_count == snap.step_count


# ---------------------------------------------------------------------------
# Adversarial: zero-step config
# ---------------------------------------------------------------------------


class TestPromptLoopZeroStepConfig:
    """Edge case: max_steps=0 blocks ALL calls immediately."""

    def test_zero_max_steps_blocks_first_call(self) -> None:
        """max_steps=0 must block even the very first call."""
        llm = LoopingStubLLM()
        config = ExecutionConfig(max_cost_usd=100.0, max_steps=0, max_retries_total=0)
        ctx = ExecutionContext(config=config)

        decision = ctx.wrap_llm_call(fn=llm.call)
        assert decision == Decision.HALT
        assert llm.call_count == 0

    def test_zero_cost_blocks_any_cost_hint(self) -> None:
        """max_cost_usd=0.0 must block first call with any positive cost hint."""
        llm = LoopingStubLLM()
        config = ExecutionConfig(max_cost_usd=0.0, max_steps=100, max_retries_total=0)
        ctx = ExecutionContext(config=config)

        decision = ctx.wrap_llm_call(
            fn=llm.call,
            options=WrapOptions(cost_estimate_hint=0.001),
        )
        assert decision == Decision.HALT
        assert llm.call_count == 0
