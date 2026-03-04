"""Tests for async veronica_guard ContextVar behavior (Task #4)."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from veronica_core.inject import (
    get_active_container,
    is_guard_active,
    veronica_guard,
)


# ---------------------------------------------------------------------------
# Test: async wrapper detection
# ---------------------------------------------------------------------------


class TestAsyncWrapperDetection:
    def test_async_fn_returns_coroutinefunction(self) -> None:
        """veronica_guard on async fn must produce a coroutinefunction."""

        @veronica_guard()
        async def async_fn() -> str:
            return "ok"

        assert inspect.iscoroutinefunction(async_fn)

    def test_sync_fn_remains_regular_function(self) -> None:
        """veronica_guard on sync fn must not produce a coroutinefunction."""

        @veronica_guard()
        def sync_fn() -> str:
            return "ok"

        assert not inspect.iscoroutinefunction(sync_fn)


# ---------------------------------------------------------------------------
# Test: async ContextVar set/reset
# ---------------------------------------------------------------------------


class TestAsyncContextVar:
    def test_guard_active_is_true_inside_async_call(self) -> None:
        """_guard_active ContextVar must be True inside async boundary."""
        seen: list[bool] = []

        @veronica_guard()
        async def inner() -> None:
            seen.append(is_guard_active())

        asyncio.run(inner())
        assert seen == [True]

    def test_guard_active_is_false_after_async_call(self) -> None:
        """_guard_active must reset to False after async call completes."""

        @veronica_guard()
        async def inner() -> None:
            pass

        assert is_guard_active() is False
        asyncio.run(inner())
        assert is_guard_active() is False

    def test_guard_active_resets_after_async_exception(self) -> None:
        """_guard_active must reset to False even if async fn raises."""

        @veronica_guard()
        async def failing() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(failing())

        assert is_guard_active() is False

    def test_active_container_available_inside_async_call(self) -> None:
        """_active_container ContextVar must be populated inside async boundary."""
        containers: list[object] = []

        @veronica_guard()
        async def inner() -> None:
            containers.append(get_active_container())

        asyncio.run(inner())
        assert len(containers) == 1
        assert containers[0] is not None

    def test_active_container_none_after_async_call(self) -> None:
        """_active_container must reset to None after async call completes."""

        @veronica_guard()
        async def inner() -> None:
            pass

        asyncio.run(inner())
        assert get_active_container() is None

    def test_async_fn_returns_correct_value(self) -> None:
        """Async wrapper must propagate the return value of the wrapped coroutine."""

        @veronica_guard()
        async def compute(x: int, y: int) -> int:
            return x + y

        result = asyncio.run(compute(3, 4))
        assert result == 7


# ---------------------------------------------------------------------------
# Test: async guard isolation across concurrent tasks
# ---------------------------------------------------------------------------


class TestAsyncConcurrentIsolation:
    def test_contextvar_isolated_across_concurrent_tasks(self) -> None:
        """ContextVar must not leak between concurrent asyncio tasks."""
        results: list[bool] = []

        @veronica_guard()
        async def check_active() -> bool:
            await asyncio.sleep(0)  # Yield to allow interleaving
            return is_guard_active()

        async def runner() -> None:
            task1 = asyncio.create_task(check_active())
            task2 = asyncio.create_task(check_active())
            r1, r2 = await asyncio.gather(task1, task2)
            results.extend([r1, r2])

        asyncio.run(runner())
        # Each task sees guard active (True) within its own context
        assert results == [True, True]
        # After all tasks complete, guard is inactive in the main context
        assert is_guard_active() is False
