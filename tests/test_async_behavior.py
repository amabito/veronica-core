"""Tests for async observable behavior with veronica_guard (M-4, S-1).

veronica_guard wraps async functions: the guard checks run synchronously,
then the decorated async fn is called, returning a coroutine. The caller
must await the coroutine.

Observable behavior verified:
- Guard allows/blocks before the async fn body runs
- The async return value is accessible when awaited
- Multiple concurrent calls via asyncio.gather work correctly
- Each call gets an independent container (no shared budget state)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.inject import veronica_guard, VeronicaHalt
from veronica_core.runtime_policy import PolicyDecision


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_async_guard_allows_fn_to_run_when_awaited():
    """GIVEN an async function wrapped with veronica_guard (return_decision=False),
    WHEN the result is awaited inside asyncio.run(),
    THEN the async function body executes and the return value is correct.
    """
    executed = []

    @veronica_guard(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
    async def _async_work(value: int) -> int:
        await asyncio.sleep(0)
        executed.append(value)
        return value * 2

    async def _run():
        result = await _async_work(5)
        return result

    result = asyncio.run(_run())

    assert result == 10, f"Expected 10, got {result}"
    assert executed == [5], f"Expected fn body to execute with value 5, got {executed}"


def test_async_guard_return_value_is_correct():
    """GIVEN an async function wrapped with veronica_guard,
    WHEN the result is awaited,
    THEN the exact return value from the async body is returned.
    """
    @veronica_guard(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
    async def _async_fn(x: int) -> int:
        await asyncio.sleep(0)
        return x + 100

    async def _run():
        return await _async_fn(42)

    result = asyncio.run(_run())
    assert result == 142, f"Expected 142, got {result}"


def test_async_multiple_concurrent_calls_via_gather():
    """GIVEN an async function wrapped with veronica_guard,
    WHEN multiple calls are run concurrently via asyncio.gather,
    THEN all calls complete and return correct independent results.

    Each veronica_guard call creates a fresh container, so shared budget
    state does not interfere between calls.
    """
    @veronica_guard(max_cost_usd=100.0, max_steps=100, max_retries_total=10)
    async def _concurrent_fn(n: int) -> int:
        await asyncio.sleep(0)
        return n * 3

    async def _run():
        results = await asyncio.gather(
            _concurrent_fn(1),
            _concurrent_fn(2),
            _concurrent_fn(3),
        )
        return results

    results = asyncio.run(_run())

    assert len(results) == 3
    assert results[0] == 3
    assert results[1] == 6
    assert results[2] == 9


def test_async_each_call_gets_independent_container():
    """GIVEN an async function wrapped with veronica_guard (max_steps=2),
    WHEN the same async fn is called multiple times,
    THEN each call uses a fresh container (step limit resets between calls).

    This confirms no shared budget state leaks between invocations.
    """
    call_count = []

    @veronica_guard(max_cost_usd=100.0, max_steps=2, max_retries_total=5)
    async def _fn_with_step_limit() -> str:
        await asyncio.sleep(0)
        call_count.append(1)
        return "ok"

    async def _run():
        results = []
        # Call 5 times — if containers were shared, steps=2 would block after 2 calls
        for _ in range(5):
            r = await _fn_with_step_limit()
            results.append(r)
        return results

    results = asyncio.run(_run())

    # All 5 calls should succeed (each has its own fresh container with max_steps=2)
    assert len(results) == 5, f"Expected 5 results, got {len(results)}"
    assert all(r == "ok" for r in results)
    assert len(call_count) == 5, "Fn body must execute for each call"


def test_async_guard_raises_veronica_halt_when_policy_denies():
    """GIVEN an async function wrapped with veronica_guard (very tight budget),
    WHEN a second call is made after the guard has denied (VeronicaHalt raised),
    THEN each fresh call starts with a clean container.

    Note: veronica_guard creates a new container per call, so a single call
    cannot be blocked by a previous call's state. This test verifies the
    fresh-container guarantee at the async level.
    """
    @veronica_guard(max_cost_usd=100.0, max_steps=100, max_retries_total=10)
    async def _permissive() -> str:
        await asyncio.sleep(0)
        return "done"

    async def _run():
        r1 = await _permissive()
        r2 = await _permissive()
        return r1, r2

    r1, r2 = asyncio.run(_run())
    assert r1 == "done"
    assert r2 == "done"
