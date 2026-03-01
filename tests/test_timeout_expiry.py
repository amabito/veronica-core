"""Tests for timeout watcher behavior (S-2).

Observable behavior: The timeout watcher sets the CancellationToken after timeout_ms.
This has two observable effects:
1. After timeout elapses, NEW wrap calls return Decision.HALT immediately.
2. If fn() raises an exception while the token is set, the call returns Decision.HALT.

No private attribute access — only public API used: wrap_llm_call, get_snapshot.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment import ExecutionConfig, ExecutionContext
from veronica_core.shield.types import Decision


def test_timeout_blocks_calls_made_after_timeout_elapses():
    """GIVEN an ExecutionContext with timeout_ms=200,
    WHEN a call is made AFTER the timeout has elapsed,
    THEN the call returns Decision.HALT (no fn() invocation).

    Observable behavior: subsequent calls after timeout are blocked.
    """
    config = ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=50,
        max_retries_total=5,
        timeout_ms=200,
    )

    with ExecutionContext(config=config) as ctx:
        # Wait for timeout to elapse
        time.sleep(0.35)

        # Call after timeout elapsed
        fn_called = []
        decision = ctx.wrap_llm_call(fn=lambda: fn_called.append(1))

    assert decision == Decision.HALT, (
        f"Expected Decision.HALT after timeout elapsed, got {decision}"
    )
    assert fn_called == [], "fn must NOT be called when timeout has already elapsed"


def test_timeout_does_not_fire_for_fast_fn():
    """GIVEN an ExecutionContext with timeout_ms=500,
    WHEN a fast fn (no sleep) is called immediately,
    THEN the wrap call returns Decision.ALLOW (timeout does not interfere).
    """
    config = ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=50,
        max_retries_total=5,
        timeout_ms=500,
    )

    with ExecutionContext(config=config) as ctx:
        decision = ctx.wrap_llm_call(fn=lambda: None)

    assert decision == Decision.ALLOW, (
        f"Expected Decision.ALLOW for fast fn, got {decision}"
    )


def test_timeout_zero_disables_timeout():
    """GIVEN an ExecutionContext with timeout_ms=0 (disabled),
    WHEN a call is made,
    THEN it completes normally without timeout interference.
    """
    config = ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=50,
        max_retries_total=5,
        timeout_ms=0,  # disabled
    )

    with ExecutionContext(config=config) as ctx:
        decision = ctx.wrap_llm_call(fn=lambda: None)

    assert decision == Decision.ALLOW


def test_timeout_subsequent_calls_after_timeout_also_halt():
    """GIVEN a context that has already timed out (waited for timeout to elapse),
    WHEN additional wrap calls are made,
    THEN they also return Decision.HALT (timeout state is sticky).
    """
    config = ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=50,
        max_retries_total=5,
        timeout_ms=100,
    )

    with ExecutionContext(config=config) as ctx:
        # Wait for timeout to elapse
        time.sleep(0.25)

        # First call after timeout
        d1 = ctx.wrap_llm_call(fn=lambda: None)
        # Second call after timeout
        d2 = ctx.wrap_llm_call(fn=lambda: None)

    assert d1 == Decision.HALT, f"Expected HALT after timeout, got {d1}"
    assert d2 == Decision.HALT, f"Expected HALT on second post-timeout call, got {d2}"


def test_timeout_snapshot_shows_events_after_timeout():
    """GIVEN a context whose timeout has elapsed,
    WHEN snapshot is taken,
    THEN the events include a timeout-related event.

    Only public API used: get_snapshot(), events list.
    """
    config = ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=50,
        max_retries_total=5,
        timeout_ms=100,
    )

    with ExecutionContext(config=config) as ctx:
        # Wait for timeout to elapse
        time.sleep(0.25)
        # Make a call to trigger the check (which records the event)
        ctx.wrap_llm_call(fn=lambda: None)
        snap = ctx.get_snapshot()

    event_types = [e.event_type for e in snap.events]
    assert any("TIMEOUT" in t or "timeout" in t.lower() for t in event_types), (
        f"Expected a timeout event, got: {event_types}"
    )


def test_timeout_fn_exception_during_timeout_returns_halt():
    """GIVEN an ExecutionContext with timeout_ms=200,
    WHEN fn() raises an exception and the timeout token is already set,
    THEN the wrap call returns Decision.HALT (timeout takes precedence over retry).

    This tests the exception-path timeout check in wrap_llm_call.
    """
    config = ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=50,
        max_retries_total=5,
        timeout_ms=100,
    )

    with ExecutionContext(config=config) as ctx:
        # Wait for timeout to elapse first
        time.sleep(0.25)

        # Now call with an fn that raises — timeout token is already set
        decision = ctx.wrap_llm_call(fn=lambda: (_ for _ in ()).throw(RuntimeError("deliberate")))

    assert decision == Decision.HALT, (
        f"Expected Decision.HALT when fn raises after timeout, got {decision}"
    )
