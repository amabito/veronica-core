"""Tests for ExecutionContext.close() and context manager cleanup.

Tests:
1. close() cancels the timeout pool handle.
2. close() is idempotent (calling twice is safe).
3. Context manager __exit__ calls close().
4. wrap_llm_call after close() returns Decision.HALT.
5. close() warns when graph nodes are still non-terminal.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment import ExecutionConfig, ExecutionContext
from veronica_core.shield.types import Decision


def _make_ctx(**kwargs: Any) -> ExecutionContext:
    """Create a minimal ExecutionContext for testing."""
    cfg = ExecutionConfig(
        max_cost_usd=kwargs.pop("max_cost_usd", 10.0),
        max_steps=kwargs.pop("max_steps", 50),
        max_retries_total=kwargs.pop("max_retries_total", 10),
        **kwargs,
    )
    return ExecutionContext(config=cfg)


# ---------------------------------------------------------------------------
# test_close_cancels_timeout
# ---------------------------------------------------------------------------


def test_close_cancels_timeout():
    """close() must cancel the timeout pool handle when one is active."""
    ctx = _make_ctx(timeout_ms=30_000)

    # A timeout handle must have been registered.
    assert ctx._timeout_pool_handle is not None, "timeout handle must be set"

    cancelled_handles: list[Any] = []

    from veronica_core.containment.timeout_pool import _timeout_pool as _pool

    original_cancel = _pool.cancel

    def _spy_cancel(handle: Any) -> None:
        cancelled_handles.append(handle)
        original_cancel(handle)

    _pool.cancel = _spy_cancel
    try:
        handle_before = ctx._timeout_pool_handle
        ctx.close()
        assert handle_before in cancelled_handles, (
            "timeout handle must be passed to pool.cancel()"
        )
        assert ctx._timeout_pool_handle is None, (
            "_timeout_pool_handle must be cleared after close()"
        )
    finally:
        _pool.cancel = original_cancel


def test_close_sets_closed_flag():
    """close() must set _closed flag and mark context as aborted."""
    ctx = _make_ctx()
    assert not getattr(ctx, "_closed", False)
    ctx.close()
    assert ctx._closed is True
    assert ctx._aborted is True


# ---------------------------------------------------------------------------
# test_close_is_idempotent
# ---------------------------------------------------------------------------


def test_close_is_idempotent():
    """Calling close() twice must not raise and must not duplicate side-effects."""
    close_count = [0]
    ctx = _make_ctx()

    # Patch _budget_backend.close to count invocations.
    original_close = ctx._budget_backend.close

    def _counting_close() -> None:
        close_count[0] += 1
        original_close()

    ctx._budget_backend.close = _counting_close

    ctx.close()
    ctx.close()  # second call must be a no-op

    assert close_count[0] == 1, (
        "budget_backend.close() must be called exactly once even if close() is called twice"
    )
    assert ctx._closed is True


def test_close_idempotent_partial_buffers_not_double_cleared():
    """close() twice must not raise even if _partial_buffers is already empty."""
    ctx = _make_ctx()
    ctx.close()
    # _partial_buffers is now empty; calling close() again must not raise.
    ctx.close()


# ---------------------------------------------------------------------------
# test_context_manager_calls_close
# ---------------------------------------------------------------------------


def test_context_manager_calls_close():
    """__exit__ must call close(), leaving the context in a closed state."""
    with _make_ctx() as ctx:
        pass  # __exit__ fires here

    assert ctx._closed is True
    assert ctx._aborted is True


def test_context_manager_calls_close_on_exception():
    """__exit__ must call close() even when an exception propagates."""
    ctx_ref: list[ExecutionContext] = []
    try:
        with _make_ctx() as ctx:
            ctx_ref.append(ctx)
            raise RuntimeError("test error")
    except RuntimeError:
        pass

    assert ctx_ref[0]._closed is True


# ---------------------------------------------------------------------------
# test_operation_after_close_raises_or_warns
# ---------------------------------------------------------------------------


def test_wrap_llm_call_after_close_returns_halt():
    """wrap_llm_call() on a closed context must return Decision.HALT without calling fn."""
    ctx = _make_ctx()
    ctx.close()

    called = []
    decision = ctx.wrap_llm_call(fn=lambda: called.append(1))

    assert decision == Decision.HALT, "closed context must return HALT"
    assert called == [], "fn must not be called on a closed context"


def test_wrap_tool_call_after_close_returns_halt():
    """wrap_tool_call() on a closed context must return Decision.HALT."""
    ctx = _make_ctx()
    ctx.close()

    called = []
    decision = ctx.wrap_tool_call(fn=lambda: called.append(1))

    assert decision == Decision.HALT
    assert called == []


# ---------------------------------------------------------------------------
# test_close_warns_on_non_terminal_nodes
# ---------------------------------------------------------------------------


def test_close_warns_on_non_terminal_nodes(caplog: pytest.LogCaptureFixture):
    """close() must emit a warning when graph nodes are still non-terminal."""
    import logging

    ctx = _make_ctx()

    # Create a node via the graph API and advance it to "running" state,
    # but do NOT call mark_success/mark_failure/mark_halt — leaving it
    # in a non-terminal state to trigger the warning.
    node_id = ctx._graph.begin_node(
        parent_id=ctx._root_node_id,
        kind="llm",
        name="test_op",
    )
    ctx._graph.mark_running(node_id)
    # node is now in "running" status — non-terminal

    with caplog.at_level(
        logging.WARNING, logger="veronica_core.containment.execution_context"
    ):
        ctx.close()

    assert any("non-terminal" in record.message for record in caplog.records), (
        "close() must log a warning when non-terminal graph nodes exist"
    )


def test_close_no_warning_when_all_nodes_terminal(caplog: pytest.LogCaptureFixture):
    """close() must NOT emit a warning when all graph nodes are in terminal state."""
    import logging

    ctx = _make_ctx()
    # Do one successful wrap call to create a terminal node.
    ctx.wrap_llm_call(fn=lambda: None)

    with caplog.at_level(
        logging.WARNING, logger="veronica_core.containment.execution_context"
    ):
        ctx.close()

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    non_terminal_warnings = [m for m in warning_msgs if "non-terminal" in m]
    assert non_terminal_warnings == [], (
        f"Unexpected non-terminal warning(s): {non_terminal_warnings}"
    )
