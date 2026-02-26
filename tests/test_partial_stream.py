"""Tests for PartialResultBuffer integration with ExecutionContext.

Covers:
- ContextVar injection during wrap_llm_call
- ContextVar cleanup after the call (success and exception)
- get_partial_result() per graph_node_id
- mark_complete() called on clean completion, not on halt/exception
- Multiple sequential calls each with their own buffer
- Existing wrap behavior is unchanged when partial_buffer=None
- attach_partial_buffer() raises outside wrap context
- NodeRecord stores the buffer reference
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment.execution_context import (
    ExecutionConfig,
    ExecutionContext,
    WrapOptions,
    attach_partial_buffer,
    get_current_partial_buffer,
)
from veronica_core.partial import PartialResultBuffer
from veronica_core.shield.types import Decision


def _make_ctx(max_steps: int = 10) -> ExecutionContext:
    cfg = ExecutionConfig(max_cost_usd=10.0, max_steps=max_steps, max_retries_total=5)
    return ExecutionContext(config=cfg)


# ---------------------------------------------------------------------------
# Test 1: ContextVar carries the buffer into fn()
# ---------------------------------------------------------------------------

def test_partial_buffer_injected_via_contextvars():
    """Inside fn(), get_current_partial_buffer() returns the buffer passed in WrapOptions."""
    ctx = _make_ctx()
    buf = PartialResultBuffer()
    captured = []

    def fn():
        captured.append(get_current_partial_buffer())

    ctx.wrap_llm_call(fn=fn, options=WrapOptions(partial_buffer=buf))

    assert len(captured) == 1
    assert captured[0] is buf


# ---------------------------------------------------------------------------
# Test 2: ContextVar is reset to None after wrap completes
# ---------------------------------------------------------------------------

def test_partial_buffer_cleared_after_wrap():
    """After wrap_llm_call completes, get_current_partial_buffer() returns None."""
    ctx = _make_ctx()
    buf = PartialResultBuffer()

    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(partial_buffer=buf))

    assert get_current_partial_buffer() is None


# ---------------------------------------------------------------------------
# Test 3: No buffer set → None inside fn()
# ---------------------------------------------------------------------------

def test_partial_buffer_none_by_default():
    """Calling wrap_llm_call with no partial_buffer yields None inside fn()."""
    ctx = _make_ctx()
    captured = []

    def fn():
        captured.append(get_current_partial_buffer())

    ctx.wrap_llm_call(fn=fn)

    assert captured[0] is None


# ---------------------------------------------------------------------------
# Test 4: get_partial_result returns the buffer object
# ---------------------------------------------------------------------------

def test_get_partial_result_returns_buffer():
    """fn() appends to buffer; ctx.get_partial_result(node_id) returns the buffer."""
    ctx = _make_ctx()
    buf = PartialResultBuffer()

    def fn():
        buf.append("hello")
        buf.append(" world")

    ctx.wrap_llm_call(fn=fn, options=WrapOptions(partial_buffer=buf))

    # Find node_id via _partial_buffers (acceptable in tests per spec).
    node_id = next(k for k, v in ctx._partial_buffers.items() if v is buf)
    result = ctx.get_partial_result(node_id)
    assert result is buf
    assert result.get_partial() == "hello world"


# ---------------------------------------------------------------------------
# Test 5: Unknown node_id returns None
# ---------------------------------------------------------------------------

def test_get_partial_result_none_for_unknown_node():
    """get_partial_result returns None for an unrecognised node_id."""
    ctx = _make_ctx()
    assert ctx.get_partial_result("nonexistent-node-id") is None


# ---------------------------------------------------------------------------
# Test 6: Two sequential calls each get their own buffer
# ---------------------------------------------------------------------------

def test_partial_buffer_not_shared_across_calls():
    """Two sequential wrap_llm_call calls each see their own buffer inside fn()."""
    ctx = _make_ctx()
    buf_a = PartialResultBuffer()
    buf_b = PartialResultBuffer()
    seen = []

    def fn_a():
        seen.append(("a", get_current_partial_buffer()))

    def fn_b():
        seen.append(("b", get_current_partial_buffer()))

    ctx.wrap_llm_call(fn=fn_a, options=WrapOptions(partial_buffer=buf_a))
    ctx.wrap_llm_call(fn=fn_b, options=WrapOptions(partial_buffer=buf_b))

    assert seen[0] == ("a", buf_a)
    assert seen[1] == ("b", buf_b)
    # After both calls the ContextVar is clear.
    assert get_current_partial_buffer() is None


# ---------------------------------------------------------------------------
# Test 7: Exception in fn() still resets ContextVar; buffer is not marked complete
# ---------------------------------------------------------------------------

def test_partial_buffer_survives_exception_in_fn():
    """fn() raises; buffer retains appended data; ContextVar is reset; is_complete stays False."""
    ctx = _make_ctx()
    buf = PartialResultBuffer()

    def fn():
        buf.append("partial")
        raise RuntimeError("boom")

    ctx.wrap_llm_call(fn=fn, options=WrapOptions(partial_buffer=buf))

    # ContextVar must be cleared even though fn raised.
    assert get_current_partial_buffer() is None
    # Buffer preserves what was appended before the exception.
    assert buf.get_partial() == "partial"
    # mark_complete() must NOT be called when fn raises.
    assert buf.is_complete is False


# ---------------------------------------------------------------------------
# Test 8: Existing wrap behavior unchanged when partial_buffer=None
# ---------------------------------------------------------------------------

def test_wrap_without_partial_buffer_unchanged():
    """Existing wrap behavior is unchanged when partial_buffer is not set."""
    ctx = _make_ctx()
    calls = []

    decision = ctx.wrap_llm_call(fn=lambda: calls.append(1))

    assert decision == Decision.ALLOW
    assert calls == [1]
    snap = ctx.get_snapshot()
    assert snap.step_count == 1


# ---------------------------------------------------------------------------
# Test 9: mark_complete() called automatically on clean success
# ---------------------------------------------------------------------------

def test_partial_buffer_marks_complete():
    """After a clean wrap_llm_call, the buffer's is_complete is True."""
    ctx = _make_ctx()
    buf = PartialResultBuffer()

    def fn():
        buf.append("done")

    ctx.wrap_llm_call(fn=fn, options=WrapOptions(partial_buffer=buf))

    assert buf.is_complete is True
    assert buf.get_partial() == "done"


# ---------------------------------------------------------------------------
# Test 10: Multiple buffers per context, get_partial_result correct per node
# ---------------------------------------------------------------------------

def test_multiple_buffers_per_context():
    """Multiple wrap calls each with a separate buffer; get_partial_result returns the correct buffer per node."""
    ctx = _make_ctx()
    buf_x = PartialResultBuffer()
    buf_y = PartialResultBuffer()

    def fn_x():
        buf_x.append("from-x")

    def fn_y():
        buf_y.append("from-y")

    ctx.wrap_llm_call(fn=fn_x, options=WrapOptions(partial_buffer=buf_x))
    ctx.wrap_llm_call(fn=fn_y, options=WrapOptions(partial_buffer=buf_y))

    # Retrieve node IDs from the internal dict (acceptable in tests).
    node_x = next(k for k, v in ctx._partial_buffers.items() if v is buf_x)
    node_y = next(k for k, v in ctx._partial_buffers.items() if v is buf_y)

    assert node_x != node_y
    result_x = ctx.get_partial_result(node_x)
    result_y = ctx.get_partial_result(node_y)
    assert result_x is buf_x
    assert result_y is buf_y
    assert result_x.get_partial() == "from-x"
    assert result_y.get_partial() == "from-y"


# ---------------------------------------------------------------------------
# Test 11: attach_partial_buffer raises outside wrap context
# ---------------------------------------------------------------------------

def test_attach_partial_buffer_raises_outside_wrap():
    """attach_partial_buffer() raises RuntimeError when called outside wrap_llm_call."""
    buf = PartialResultBuffer()
    try:
        attach_partial_buffer(buf)
        assert False, "Expected RuntimeError"
    except RuntimeError as e:
        assert "outside" in str(e).lower() or "wrap" in str(e).lower()


# ---------------------------------------------------------------------------
# Test 12: NodeRecord stores the buffer reference
# ---------------------------------------------------------------------------

def test_node_record_stores_partial_buffer():
    """The NodeRecord for a wrap call has partial_buffer set to the passed buffer."""
    ctx = _make_ctx()
    buf = PartialResultBuffer()

    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(partial_buffer=buf))

    snap = ctx.get_snapshot()
    assert len(snap.nodes) == 1
    assert snap.nodes[0].partial_buffer is buf
