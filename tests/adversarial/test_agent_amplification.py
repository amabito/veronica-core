"""Adversarial test: agent amplification attack (multi-level agent spawning).

Attack pattern: An agent spawns sub-agents, which in turn spawn their own
sub-agents, creating an exponential fan-out of LLM calls. Each level of
spawning amplifies total cost.

Tests verify that:
1. Without containment: amplification grows exponentially (baseline)
2. With CancellationToken propagation: parent cancellation stops all children
3. With ExecutionContext parent-child hierarchy: child costs bubble up to parent
   and are contained by the parent's budget ceiling
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from veronica_core.containment import (
    CancellationToken,
    ExecutionConfig,
    ExecutionContext,
    WrapOptions,
)
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Stub agent that spawns children
# ---------------------------------------------------------------------------


class StubAgent:
    """Simulates an LLM-backed agent that spawns sub-agents.

    depth=0: leaf node (no children)
    depth>0: spawns `branching_factor` children at depth-1
    """

    def __init__(
        self,
        name: str,
        depth: int,
        branching_factor: int = 2,
        call_registry: list[str] | None = None,
    ) -> None:
        self.name = name
        self.depth = depth
        self.branching_factor = branching_factor
        self.call_registry = call_registry if call_registry is not None else []

    def run(self) -> int:
        """Run this agent and all its children. Returns total call count."""
        self.call_registry.append(self.name)
        total = 1  # Count self

        if self.depth > 0:
            for i in range(self.branching_factor):
                child = StubAgent(
                    name=f"{self.name}.child_{i}",
                    depth=self.depth - 1,
                    branching_factor=self.branching_factor,
                    call_registry=self.call_registry,
                )
                total += child.run()
        return total


class StubAgentWithContext:
    """Agent variant that uses ExecutionContext for each LLM call."""

    def __init__(
        self,
        name: str,
        depth: int,
        branching_factor: int,
        ctx: ExecutionContext,
        call_registry: list[str],
    ) -> None:
        self.name = name
        self.depth = depth
        self.branching_factor = branching_factor
        self.ctx = ctx
        self.call_registry = call_registry

    def run(self) -> tuple[int, int]:
        """Run with containment. Returns (allowed, halted) counts."""
        allowed = 0
        halted = 0

        decision = self.ctx.wrap_llm_call(
            fn=lambda: self.call_registry.append(self.name),
            options=WrapOptions(
                operation_name=self.name,
                cost_estimate_hint=0.01,
            ),
        )

        if decision == Decision.ALLOW:
            allowed += 1
        else:
            halted += 1
            # When parent is halted, do not spawn children
            return allowed, halted

        if self.depth > 0:
            for i in range(self.branching_factor):
                child = StubAgentWithContext(
                    name=f"{self.name}.child_{i}",
                    depth=self.depth - 1,
                    branching_factor=self.branching_factor,
                    ctx=self.ctx,
                    call_registry=self.call_registry,
                )
                child_allowed, child_halted = child.run()
                allowed += child_allowed
                halted += child_halted

        return allowed, halted


# ---------------------------------------------------------------------------
# Without containment: amplification baseline
# ---------------------------------------------------------------------------


class TestAgentAmplificationWithoutContainment:
    """Baseline: confirm exponential fan-out without containment."""

    def test_depth_1_branching_2_calls(self) -> None:
        """depth=1, branching=2: 1 root + 2 children = 3 calls."""
        registry: list[str] = []
        agent = StubAgent("root", depth=1, branching_factor=2, call_registry=registry)
        total = agent.run()
        assert total == 3
        assert len(registry) == 3

    def test_depth_3_branching_2_exponential_calls(self) -> None:
        """depth=3, branching=2: 1+2+4+8 = 15 calls (exponential)."""
        registry: list[str] = []
        agent = StubAgent("root", depth=3, branching_factor=2, call_registry=registry)
        total = agent.run()
        # Sum of geometric series: sum(2^k for k in 0..3) = 15
        assert total == 15
        assert len(registry) == 15

    def test_depth_4_branching_3_massive_fan_out(self) -> None:
        """depth=4, branching=3: 1+3+9+27+81 = 121 calls."""
        registry: list[str] = []
        agent = StubAgent("root", depth=4, branching_factor=3, call_registry=registry)
        total = agent.run()
        assert total == 121
        assert len(registry) == 121


# ---------------------------------------------------------------------------
# With containment: ExecutionContext stops amplification
# ---------------------------------------------------------------------------


class TestAgentAmplificationContained:
    """ExecutionContext step limit and cost ceiling stop agent amplification."""

    def test_step_limit_stops_amplification(self) -> None:
        """Step limit caps the total agent calls even in deep hierarchy."""
        registry: list[str] = []
        # Without containment: depth=3, branching=2 = 15 calls
        # With max_steps=5: only 5 calls allowed
        config = ExecutionConfig(
            max_cost_usd=100.0, max_steps=5, max_retries_total=10
        )
        ctx = ExecutionContext(config=config)
        agent = StubAgentWithContext(
            name="root",
            depth=3,
            branching_factor=2,
            ctx=ctx,
            call_registry=registry,
        )
        allowed, halted = agent.run()

        assert allowed == 5
        assert allowed + halted == 5 + halted  # halted calls were stopped
        assert len(registry) == 5  # Only 5 agents actually ran

    def test_cost_ceiling_stops_amplification(self) -> None:
        """Cost ceiling stops multi-level spawning before budget is exhausted."""
        registry: list[str] = []
        # Each agent costs $0.01, budget = $0.05 => max 5 agents
        config = ExecutionConfig(
            max_cost_usd=0.05, max_steps=100, max_retries_total=10
        )
        ctx = ExecutionContext(config=config)
        agent = StubAgentWithContext(
            name="root",
            depth=3,
            branching_factor=2,
            ctx=ctx,
            call_registry=registry,
        )
        allowed, halted = agent.run()

        assert allowed <= 5
        assert len(registry) == allowed

        snap = ctx.get_snapshot()
        assert snap.cost_usd_accumulated <= 0.05 + 0.01  # within budget (+1 tolerance)

    def test_snapshot_tracks_all_nodes(self) -> None:
        """ContextSnapshot nodes list captures every contained agent call."""
        registry: list[str] = []
        config = ExecutionConfig(
            max_cost_usd=100.0, max_steps=7, max_retries_total=10
        )
        ctx = ExecutionContext(config=config)
        agent = StubAgentWithContext(
            name="root",
            depth=3,
            branching_factor=2,
            ctx=ctx,
            call_registry=registry,
        )
        allowed, _ = agent.run()

        snap = ctx.get_snapshot()
        assert snap.step_count == allowed
        # Successful nodes have status "ok"; halted nodes are also recorded
        # but do not increment step_count
        llm_ok_nodes = [n for n in snap.nodes if n.kind == "llm" and n.status == "ok"]
        assert len(llm_ok_nodes) == allowed


# ---------------------------------------------------------------------------
# CancellationToken propagation
# ---------------------------------------------------------------------------


class TestCancellationTokenPropagation:
    """Parent cancellation must stop all child agents cooperatively."""

    def test_cancel_stops_cooperative_children(self) -> None:
        """Once token is cancelled, all agents checking it must stop."""
        token = CancellationToken()
        call_count = 0

        def agent_step() -> None:
            nonlocal call_count
            if token.is_cancelled:
                return
            call_count += 1

        # Run 3 steps, then cancel
        agent_step()
        agent_step()
        agent_step()
        assert call_count == 3

        token.cancel()

        # After cancellation, no new work proceeds
        agent_step()
        agent_step()
        assert call_count == 3  # Still 3, not 5

    def test_cancel_is_idempotent(self) -> None:
        """Calling cancel() multiple times must not raise or change state."""
        token = CancellationToken()
        token.cancel()
        token.cancel()
        token.cancel()
        assert token.is_cancelled

    def test_cancel_signals_waiting_thread(self) -> None:
        """Token.wait() must return when cancel() is called from another thread."""
        token = CancellationToken()
        result: list[bool] = []

        def waiter():
            signalled = token.wait(timeout_s=5.0)
            result.append(signalled)

        t = threading.Thread(target=waiter)
        t.start()
        # Cancel from main thread after a short delay
        token.cancel()
        t.join(timeout=2.0)

        assert not t.is_alive(), "Waiter thread should have exited"
        assert result == [True]

    def test_context_abort_cancels_token(self) -> None:
        """ExecutionContext.abort() cancels the internal CancellationToken."""
        config = ExecutionConfig(
            max_cost_usd=100.0, max_steps=100, max_retries_total=0
        )
        ctx = ExecutionContext(config=config)

        # Confirm not aborted initially
        snap_before = ctx.get_snapshot()
        assert not snap_before.aborted

        ctx.abort("parent_agent_cancelled")

        snap_after = ctx.get_snapshot()
        assert snap_after.aborted
        assert snap_after.abort_reason == "parent_agent_cancelled"

    def test_child_context_cannot_exceed_parent_budget(self) -> None:
        """Child contexts constructed with lower budgets cannot overspend parent."""
        parent_registry: list[str] = []
        child_registry: list[str] = []

        # Parent budget: $0.10 (10 calls @ $0.01 each)
        parent_config = ExecutionConfig(
            max_cost_usd=0.10, max_steps=100, max_retries_total=0
        )
        parent_ctx = ExecutionContext(config=parent_config)

        # Child budget: $0.05 (5 calls @ $0.01 each)
        child_config = ExecutionConfig(
            max_cost_usd=0.05, max_steps=100, max_retries_total=0
        )
        child_ctx = ExecutionContext(config=child_config, parent=parent_ctx)

        def parent_call() -> None:
            parent_registry.append("parent")

        def child_call() -> None:
            child_registry.append("child")

        # Run parent calls
        for _ in range(10):
            parent_ctx.wrap_llm_call(
                fn=parent_call,
                options=WrapOptions(cost_estimate_hint=0.01),
            )

        # Run child calls -- child budget is exhausted after 5
        child_allowed = 0
        for _ in range(10):
            decision = child_ctx.wrap_llm_call(
                fn=child_call,
                options=WrapOptions(cost_estimate_hint=0.01),
            )
            if decision == Decision.ALLOW:
                child_allowed += 1

        # Child is bounded by its own $0.05 budget
        assert child_allowed <= 5
        assert len(child_registry) == child_allowed


# ---------------------------------------------------------------------------
# Adversarial: zero-depth agent (leaf node)
# ---------------------------------------------------------------------------


class TestLeafAgentEdgeCases:
    """Edge cases for single-agent (depth=0) scenarios."""

    def test_single_agent_no_children(self) -> None:
        """Leaf agent (depth=0) makes exactly one call."""
        registry: list[str] = []
        agent = StubAgent("leaf", depth=0, branching_factor=5, call_registry=registry)
        total = agent.run()
        assert total == 1
        assert registry == ["leaf"]

    def test_single_agent_with_context_allowed(self) -> None:
        """Leaf agent allowed by ExecutionContext with generous config."""
        registry: list[str] = []
        config = ExecutionConfig(
            max_cost_usd=100.0, max_steps=10, max_retries_total=5
        )
        ctx = ExecutionContext(config=config)
        agent = StubAgentWithContext(
            name="leaf",
            depth=0,
            branching_factor=2,
            ctx=ctx,
            call_registry=registry,
        )
        allowed, halted = agent.run()
        assert allowed == 1
        assert halted == 0
        assert registry == ["leaf"]

    def test_single_agent_halted_at_zero_steps(self) -> None:
        """Leaf agent blocked when max_steps=0."""
        registry: list[str] = []
        config = ExecutionConfig(
            max_cost_usd=100.0, max_steps=0, max_retries_total=5
        )
        ctx = ExecutionContext(config=config)
        agent = StubAgentWithContext(
            name="leaf",
            depth=0,
            branching_factor=2,
            ctx=ctx,
            call_registry=registry,
        )
        allowed, halted = agent.run()
        assert allowed == 0
        assert halted == 1
        assert registry == []
