"""Tests for P2-2: Multi-agent Context Linking.

Verifies that parent-child ExecutionContext pairs correctly propagate
costs and budget exhaustion up the chain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment import ExecutionConfig, ExecutionContext


def make_config(max_cost: float = 1.0, max_steps: int = 50) -> ExecutionConfig:
    return ExecutionConfig(max_cost_usd=max_cost, max_steps=max_steps, max_retries_total=10)


def test_no_parent_backward_compat() -> None:
    """Existing code without parent works identically."""
    config = make_config()
    with ExecutionContext(config) as ctx:
        assert ctx._parent is None
        snap = ctx.get_snapshot()
        assert snap.parent_chain_id is None


def test_parent_chain_id_in_snapshot() -> None:
    """Child snapshot contains the parent's chain_id."""
    parent_cfg = make_config()
    with ExecutionContext(parent_cfg) as parent_ctx:
        child_cfg = make_config(0.5)
        with ExecutionContext(child_cfg, parent=parent_ctx) as child_ctx:
            snap = child_ctx.get_snapshot()
            assert snap.parent_chain_id == parent_ctx.get_snapshot().chain_id


def test_child_cost_propagates_to_parent() -> None:
    """Cost propagated via _propagate_child_cost is visible in parent snapshot."""
    parent_cfg = make_config(max_cost=1.0)
    with ExecutionContext(parent_cfg) as parent_ctx:
        child_cfg = make_config(max_cost=0.5)
        with ExecutionContext(child_cfg, parent=parent_ctx) as child_ctx:
            child_ctx._propagate_child_cost(0.3)
        snap = parent_ctx.get_snapshot()
        assert snap.cost_usd_accumulated == pytest.approx(0.3, abs=0.001)


def test_parent_budget_exceeded_by_child_marks_aborted() -> None:
    """When child propagation pushes parent over ceiling, parent is aborted."""
    parent_cfg = make_config(max_cost=0.5)
    with ExecutionContext(parent_cfg) as parent_ctx:
        child_cfg = make_config(max_cost=1.0)
        with ExecutionContext(child_cfg, parent=parent_ctx) as child_ctx:
            child_ctx._propagate_child_cost(0.6)  # exceeds parent's 0.5
        assert parent_ctx._aborted is True


def test_spawn_child_creates_linked_context() -> None:
    """spawn_child returns a context with parent set to self."""
    parent_cfg = make_config(max_cost=1.0)
    with ExecutionContext(parent_cfg) as parent_ctx:
        child = parent_ctx.spawn_child(max_cost_usd=0.5)
        assert child._parent is parent_ctx
        assert child._config.max_cost_usd == pytest.approx(0.5, abs=0.001)


def test_spawn_child_inherits_remaining_budget() -> None:
    """spawn_child with no max_cost_usd uses the parent's remaining budget."""
    parent_cfg = make_config(max_cost=1.0)
    with ExecutionContext(parent_cfg) as parent_ctx:
        parent_ctx._cost_usd_accumulated = 0.3  # simulate spending
        child = parent_ctx.spawn_child()  # no max specified
        assert child._config.max_cost_usd == pytest.approx(0.7, abs=0.001)


def test_spawn_child_as_context_manager() -> None:
    """spawn_child result can be used as a context manager."""
    parent_cfg = make_config(max_cost=1.0)
    with ExecutionContext(parent_cfg) as parent_ctx:
        with parent_ctx.spawn_child(max_cost_usd=0.5) as child_ctx:
            assert child_ctx._parent is parent_ctx


def test_three_level_chain() -> None:
    """Cost propagates through a three-level A->B->C chain."""
    a_cfg = make_config(max_cost=1.0)
    with ExecutionContext(a_cfg) as ctx_a:
        with ctx_a.spawn_child(max_cost_usd=0.8) as ctx_b:
            with ctx_b.spawn_child(max_cost_usd=0.5) as ctx_c:
                ctx_c._propagate_child_cost(0.2)
            assert ctx_b.get_snapshot().cost_usd_accumulated == pytest.approx(0.2, abs=0.001)
        assert ctx_a.get_snapshot().cost_usd_accumulated == pytest.approx(0.2, abs=0.001)


def test_parent_aborted_prevents_further_wrap() -> None:
    """After parent is aborted by child propagation, wrap calls return HALT."""
    from veronica_core.shield.types import Decision

    parent_cfg = make_config(max_cost=0.1)
    with ExecutionContext(parent_cfg) as parent_ctx:
        parent_ctx._propagate_child_cost(0.15)  # abort parent
        assert parent_ctx._aborted is True
        # Subsequent wrap calls must HALT without executing fn.
        called = []
        decision = parent_ctx.wrap_llm_call(fn=lambda: called.append(1))
        assert decision == Decision.HALT
        assert called == []


def test_spawn_child_inherits_step_and_retry_limits() -> None:
    """spawn_child inherits max_steps and max_retries_total from parent when not specified."""
    parent_cfg = make_config(max_cost=1.0, max_steps=25)
    with ExecutionContext(parent_cfg) as parent_ctx:
        child = parent_ctx.spawn_child()
        assert child._config.max_steps == 25
        assert child._config.max_retries_total == parent_cfg.max_retries_total


def test_no_parent_snapshot_parent_chain_id_none() -> None:
    """Standalone context has parent_chain_id=None in snapshot."""
    cfg = make_config()
    with ExecutionContext(cfg) as ctx:
        snap = ctx.get_snapshot()
        assert snap.parent_chain_id is None


def test_child_propagate_zero_cost_no_abort() -> None:
    """Propagating zero cost does not abort the parent."""
    parent_cfg = make_config(max_cost=0.1)
    with ExecutionContext(parent_cfg) as parent_ctx:
        child_cfg = make_config(max_cost=0.5)
        with ExecutionContext(child_cfg, parent=parent_ctx) as child_ctx:
            child_ctx._propagate_child_cost(0.0)
        assert parent_ctx._aborted is False
        snap = parent_ctx.get_snapshot()
        assert snap.cost_usd_accumulated == pytest.approx(0.0, abs=0.001)
