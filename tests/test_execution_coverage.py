"""Coverage gap tests for ExecutionContext and CancellationToken.

Targets uncovered paths to bring total coverage to >= 90%:
- CancellationToken.cancel() / wait()
- ExecutionContext used as context manager (with statement)
- record_event()
- abort() idempotency
- Exception path returning RETRY
- retry_budget_exceeded limit
- timeout_ms > 0 starts watcher thread
- cost_estimate_hint exceeded check
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.containment.execution_context import CancellationToken
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# CancellationToken
# ---------------------------------------------------------------------------


def test_cancellation_token_cancel_and_is_cancelled():
    token = CancellationToken()
    assert not token.is_cancelled
    token.cancel()
    assert token.is_cancelled


def test_cancellation_token_cancel_is_idempotent():
    token = CancellationToken()
    token.cancel()
    token.cancel()  # must not raise
    assert token.is_cancelled


def test_cancellation_token_wait_returns_true_when_cancelled():
    token = CancellationToken()
    token.cancel()
    result = token.wait(timeout_s=0.01)
    assert result is True


def test_cancellation_token_wait_returns_false_on_timeout():
    token = CancellationToken()
    result = token.wait(timeout_s=0.01)  # token not cancelled, times out
    assert result is False


# ---------------------------------------------------------------------------
# Context manager (__enter__ / __exit__)
# ---------------------------------------------------------------------------


def test_context_manager_enter_returns_ctx():
    config = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config)
    entered = ctx.__enter__()
    assert entered is ctx
    ctx.__exit__(None, None, None)


def test_context_manager_with_statement():
    config = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=5)
    with ExecutionContext(config=config) as ctx:
        decision = ctx.wrap_llm_call(fn=lambda: None)
    assert decision == Decision.ALLOW


def test_context_manager_cancels_token_on_exit():
    config = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config)
    with ctx:
        pass
    # After exit, cancellation token must be signalled (stops timeout watcher).
    assert ctx._cancellation_token.is_cancelled


# ---------------------------------------------------------------------------
# timeout_ms > 0 starts watcher thread
# ---------------------------------------------------------------------------


def test_timeout_ms_starts_watcher_thread():
    config = ExecutionConfig(
        max_cost_usd=1.0,
        max_steps=10,
        max_retries_total=5,
        timeout_ms=5000,  # > 0 triggers watcher
    )
    ctx = ExecutionContext(config=config)
    assert ctx._timeout_thread is not None
    assert ctx._timeout_thread.is_alive()
    ctx._cancellation_token.cancel()  # unblock watcher


# ---------------------------------------------------------------------------
# record_event
# ---------------------------------------------------------------------------


def test_record_event_appends_to_event_log():
    config = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config)
    evt = SafetyEvent(
        event_type="custom_event",
        decision=Decision.ALLOW,
        reason="test",
        hook="TestHook",
    )
    ctx.record_event(evt)
    snap = ctx.get_snapshot()
    assert any(e.event_type == "custom_event" for e in snap.events)


# ---------------------------------------------------------------------------
# abort()
# ---------------------------------------------------------------------------


def test_abort_prevents_future_calls():
    config = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config)
    ctx.abort("test abort")

    called = []
    decision = ctx.wrap_llm_call(fn=lambda: called.append(1))
    assert decision == Decision.HALT
    assert called == [], "fn must not be called after abort()"


def test_abort_is_idempotent():
    config = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config)
    ctx.abort("first abort")
    ctx.abort("second abort")  # must not raise

    snap = ctx.get_snapshot()
    assert snap.aborted is True
    assert snap.abort_reason == "first abort"  # first reason preserved


def test_abort_emits_chain_event():
    config = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config)
    ctx.abort("deliberate abort")
    snap = ctx.get_snapshot()
    assert any(e.event_type == "CHAIN_ABORTED" for e in snap.events)


# ---------------------------------------------------------------------------
# Exception path: function raises -> RETRY
# ---------------------------------------------------------------------------


def test_wrap_exception_returns_retry():
    config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
    ctx = ExecutionContext(config=config)

    def fail():
        raise ValueError("simulated failure")

    decision = ctx.wrap_llm_call(fn=fail)
    assert decision == Decision.RETRY


def test_wrap_tool_exception_returns_retry():
    config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
    ctx = ExecutionContext(config=config)

    def fail():
        raise RuntimeError("tool failure")

    decision = ctx.wrap_tool_call(fn=fail)
    assert decision == Decision.RETRY


def test_wrap_exception_increments_retries_used():
    config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
    ctx = ExecutionContext(config=config)

    def fail():
        raise ValueError("boom")

    ctx.wrap_llm_call(fn=fail)
    snap = ctx.get_snapshot()
    assert snap.retries_used == 1


# ---------------------------------------------------------------------------
# retry_budget_exceeded limit
# ---------------------------------------------------------------------------


def test_retry_budget_exceeded_halts():
    config = ExecutionConfig(
        max_cost_usd=10.0,
        max_steps=50,
        max_retries_total=2,  # very tight budget
    )
    ctx = ExecutionContext(config=config)

    def fail():
        raise ValueError("boom")

    ctx.wrap_llm_call(fn=fail)  # retry 1
    ctx.wrap_llm_call(fn=fail)  # retry 2

    # Now retry budget exhausted; next call should HALT even before fn runs
    called = []
    decision = ctx.wrap_llm_call(fn=lambda: called.append(1))
    assert decision == Decision.HALT
    assert called == []


# ---------------------------------------------------------------------------
# cost_estimate_hint exceeded check
# ---------------------------------------------------------------------------


def test_cost_estimate_hint_too_large_halts():
    config = ExecutionConfig(
        max_cost_usd=0.50,
        max_steps=10,
        max_retries_total=5,
    )
    ctx = ExecutionContext(config=config)

    called = []
    decision = ctx.wrap_llm_call(
        fn=lambda: called.append(1),
        options=WrapOptions(cost_estimate_hint=1.00),  # exceeds 0.50 ceiling
    )
    assert decision == Decision.HALT
    assert called == [], "fn must not be called when estimate exceeds ceiling"


# ---------------------------------------------------------------------------
# step limit
# ---------------------------------------------------------------------------


def test_step_limit_halts():
    config = ExecutionConfig(max_cost_usd=10.0, max_steps=2, max_retries_total=5)
    ctx = ExecutionContext(config=config)

    ctx.wrap_llm_call(fn=lambda: None)
    ctx.wrap_llm_call(fn=lambda: None)

    called = []
    decision = ctx.wrap_llm_call(fn=lambda: called.append(1))
    assert decision == Decision.HALT
    assert called == []


# ---------------------------------------------------------------------------
# cost ceiling (accumulated cost)
# ---------------------------------------------------------------------------


def test_accumulated_cost_ceiling_halts():
    config = ExecutionConfig(
        max_cost_usd=0.05,
        max_steps=50,
        max_retries_total=5,
    )
    ctx = ExecutionContext(config=config)

    # Each call charges 0.03 USD via cost_estimate_hint
    ctx.wrap_llm_call(
        fn=lambda: None,
        options=WrapOptions(cost_estimate_hint=0.03),
    )

    # Second call: accumulated=0.03, check_limits sees 0.03 < 0.05 (pass),
    # but then the hint 0.03 would bring projected to 0.06 > 0.05 (halt via hint check).
    called = []
    decision = ctx.wrap_llm_call(
        fn=lambda: called.append(1),
        options=WrapOptions(cost_estimate_hint=0.03),
    )
    assert decision == Decision.HALT
    assert called == []
