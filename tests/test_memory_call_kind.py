"""Tests for ToolCallContext.kind extension: memory_read / memory_write.

Covers:
  - wrap_memory_call("memory_read") creates a correct graph node
  - wrap_memory_call("memory_write") creates a correct graph node
  - Memory calls increment step count
  - Memory calls respect step limit (HALT when exceeded)
  - Memory calls appear in snapshot.nodes with correct kind
  - Backward compat: wrap_llm_call and wrap_tool_call unchanged
  - Concurrent memory + llm calls (5 threads each)
  - Adversarial: 50 memory_write calls at step limit boundary
  - Memory governor integration: deny-all blocks memory_write
  - Memory governor integration: allow-all permits memory_read
  - Invalid kind value handling
  - Memory calls bypass ShieldPipeline before_tool_call
"""

from __future__ import annotations

import threading
from typing import Any  # noqa: F401

import pytest

from veronica_core.containment import ExecutionConfig, ExecutionContext
from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.hooks import DefaultMemoryGovernanceHook, DenyAllMemoryGovernanceHook
from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    max_steps: int = 50,
    max_cost_usd: float = 100.0,
    memory_governor: MemoryGovernor | None = None,
    pipeline: ShieldPipeline | None = None,
) -> ExecutionContext:
    cfg = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=max_steps,
        max_retries_total=10,
    )
    return ExecutionContext(
        config=cfg,
        memory_governor=memory_governor,
        pipeline=pipeline,
    )


def _noop() -> None:
    pass


# ---------------------------------------------------------------------------
# Core node creation tests
# ---------------------------------------------------------------------------


def test_memory_read_creates_graph_node_with_correct_kind() -> None:
    """wrap_memory_call("memory_read") must produce a graph node with kind='memory_read'."""
    ctx = _make_ctx()
    decision = ctx.wrap_memory_call(fn=_noop, kind="memory_read")
    assert decision == Decision.ALLOW

    graph_snap = ctx.get_graph_snapshot()
    memory_nodes = [
        n
        for n in graph_snap["nodes"].values()
        if n["kind"] == "memory_read"
    ]
    assert len(memory_nodes) == 1
    assert memory_nodes[0]["status"] == "success"


def test_memory_write_creates_graph_node_with_correct_kind() -> None:
    """wrap_memory_call("memory_write") must produce a graph node with kind='memory_write'."""
    ctx = _make_ctx()
    decision = ctx.wrap_memory_call(fn=_noop, kind="memory_write")
    assert decision == Decision.ALLOW

    graph_snap = ctx.get_graph_snapshot()
    memory_nodes = [
        n
        for n in graph_snap["nodes"].values()
        if n["kind"] == "memory_write"
    ]
    assert len(memory_nodes) == 1
    assert memory_nodes[0]["status"] == "success"


def test_memory_read_appears_in_snapshot_nodes() -> None:
    """Memory nodes must be recorded in ContextSnapshot.nodes with correct kind."""
    ctx = _make_ctx()
    ctx.wrap_memory_call(fn=_noop, kind="memory_read")
    snap = ctx.get_snapshot()

    memory_nodes = [n for n in snap.nodes if n.kind == "memory_read"]
    assert len(memory_nodes) == 1
    assert memory_nodes[0].status == "ok"


def test_memory_write_appears_in_snapshot_nodes() -> None:
    """Memory write nodes must be recorded in ContextSnapshot.nodes."""
    ctx = _make_ctx()
    ctx.wrap_memory_call(fn=_noop, kind="memory_write")
    snap = ctx.get_snapshot()

    memory_nodes = [n for n in snap.nodes if n.kind == "memory_write"]
    assert len(memory_nodes) == 1
    assert memory_nodes[0].status == "ok"


# ---------------------------------------------------------------------------
# Step counting tests
# ---------------------------------------------------------------------------


def test_memory_call_increments_step_count() -> None:
    """Each memory call (read or write) must increment step_count by 1."""
    ctx = _make_ctx(max_steps=20)
    assert ctx._step_count == 0

    ctx.wrap_memory_call(fn=_noop, kind="memory_read")
    assert ctx._step_count == 1

    ctx.wrap_memory_call(fn=_noop, kind="memory_write")
    assert ctx._step_count == 2


def test_memory_call_halts_when_step_limit_exceeded() -> None:
    """Memory calls must return HALT once max_steps is reached."""
    ctx = _make_ctx(max_steps=2)
    ctx.wrap_memory_call(fn=_noop, kind="memory_read")
    ctx.wrap_memory_call(fn=_noop, kind="memory_write")

    # Third call must be denied.
    decision = ctx.wrap_memory_call(fn=_noop, kind="memory_read")
    assert decision == Decision.HALT


def test_memory_calls_share_step_budget_with_llm_and_tool() -> None:
    """Step budget is shared across llm, tool, and memory calls."""
    ctx = _make_ctx(max_steps=3)
    ctx.wrap_llm_call(fn=_noop)
    ctx.wrap_tool_call(fn=_noop)
    ctx.wrap_memory_call(fn=_noop, kind="memory_write")

    # Fourth call (any kind) must HALT.
    assert ctx.wrap_memory_call(fn=_noop, kind="memory_read") == Decision.HALT


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_wrap_llm_call_unchanged() -> None:
    """wrap_llm_call must still work correctly after the memory_kind extension."""
    ctx = _make_ctx()
    decision = ctx.wrap_llm_call(fn=_noop)
    assert decision == Decision.ALLOW

    snap = ctx.get_snapshot()
    assert any(n.kind == "llm" for n in snap.nodes)


def test_wrap_tool_call_unchanged() -> None:
    """wrap_tool_call must still work correctly after the memory_kind extension."""
    ctx = _make_ctx()
    decision = ctx.wrap_tool_call(fn=_noop)
    assert decision == Decision.ALLOW

    snap = ctx.get_snapshot()
    assert any(n.kind == "tool" for n in snap.nodes)


# ---------------------------------------------------------------------------
# Pipeline bypass test
# ---------------------------------------------------------------------------


def test_memory_call_bypasses_shield_pipeline_before_tool_call() -> None:
    """Memory calls must not invoke ShieldPipeline.before_tool_call."""
    before_tool_calls: list[Any] = []

    class _CapturePipeline:
        def before_llm_call(self, ctx: Any) -> Decision:
            return Decision.ALLOW

        def before_tool_call(self, ctx: Any) -> Decision:
            before_tool_calls.append(ctx)
            return Decision.ALLOW

        def before_charge(self, ctx: Any, cost_usd: float) -> Decision:
            return Decision.ALLOW

        def on_error(self, ctx: Any, exc: Exception) -> Decision:
            return Decision.RETRY

        def get_events(self) -> list:
            return []

    pipeline = _CapturePipeline()  # type: ignore[arg-type]
    ctx = _make_ctx(pipeline=pipeline)  # type: ignore[arg-type]

    ctx.wrap_memory_call(fn=_noop, kind="memory_write")
    ctx.wrap_memory_call(fn=_noop, kind="memory_read")

    assert before_tool_calls == [], "before_tool_call must not be invoked for memory calls"


# ---------------------------------------------------------------------------
# Concurrent access test
# ---------------------------------------------------------------------------


def test_concurrent_memory_and_llm_calls() -> None:
    """5 memory_read + 5 llm threads must all complete without corruption."""
    ctx = _make_ctx(max_steps=100)
    results: list[Decision] = []
    lock = threading.Lock()

    def do_memory() -> None:
        d = ctx.wrap_memory_call(fn=_noop, kind="memory_read")
        with lock:
            results.append(d)

    def do_llm() -> None:
        d = ctx.wrap_llm_call(fn=_noop)
        with lock:
            results.append(d)

    threads = [threading.Thread(target=do_memory) for _ in range(5)]
    threads += [threading.Thread(target=do_llm) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 10
    assert all(d == Decision.ALLOW for d in results)
    assert ctx._step_count == 10


# ---------------------------------------------------------------------------
# Adversarial: boundary
# ---------------------------------------------------------------------------


def test_adversarial_memory_write_at_step_limit_boundary() -> None:
    """50 memory_write calls at max_steps=50 boundary -- exactly 50 allowed, rest HALT."""
    limit = 50
    ctx = _make_ctx(max_steps=limit)

    allowed = 0
    halted = 0
    for _ in range(limit + 5):
        d = ctx.wrap_memory_call(fn=_noop, kind="memory_write")
        if d == Decision.ALLOW:
            allowed += 1
        else:
            halted += 1

    assert allowed == limit
    assert halted == 5


# ---------------------------------------------------------------------------
# Memory governor integration
# ---------------------------------------------------------------------------


def test_deny_all_governor_blocks_memory_write() -> None:
    """A deny-all MemoryGovernor must block memory_write and return HALT."""
    gov = MemoryGovernor(fail_closed=True)
    gov.add_hook(DenyAllMemoryGovernanceHook())
    ctx = _make_ctx(memory_governor=gov)

    called: list[int] = []
    decision = ctx.wrap_memory_call(fn=lambda: called.append(1), kind="memory_write")

    assert decision == Decision.HALT
    assert called == [], "fn must not be called when governor denies"


def test_allow_all_governor_permits_memory_read() -> None:
    """An allow-all MemoryGovernor must permit memory_read."""
    gov = MemoryGovernor(fail_closed=False)
    gov.add_hook(DefaultMemoryGovernanceHook())
    ctx = _make_ctx(memory_governor=gov)

    called: list[int] = []
    decision = ctx.wrap_memory_call(fn=lambda: called.append(1), kind="memory_read")

    assert decision == Decision.ALLOW
    assert called == [1], "fn must be called when governor allows"


def test_deny_all_governor_blocks_memory_read() -> None:
    """A deny-all MemoryGovernor must also block memory_read."""
    gov = MemoryGovernor(fail_closed=True)
    gov.add_hook(DenyAllMemoryGovernanceHook())
    ctx = _make_ctx(memory_governor=gov)

    decision = ctx.wrap_memory_call(fn=_noop, kind="memory_read")
    assert decision == Decision.HALT


# ---------------------------------------------------------------------------
# Invalid kind handling
# ---------------------------------------------------------------------------


def test_invalid_kind_raises_type_error() -> None:
    """Passing an invalid kind string must raise TypeError or ValueError, not silently corrupt."""
    ctx = _make_ctx()

    # The type system prevents this at static check time, but runtime guard is also needed.
    with pytest.raises((TypeError, ValueError, KeyError)):
        ctx.wrap_memory_call(fn=_noop, kind="bad_kind")  # type: ignore[arg-type]
