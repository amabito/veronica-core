"""Adversarial tests for MCP adapters (v1.7.0).

Covers edge cases and attacker-mindset scenarios for both
AsyncMCPContainmentAdapter and MCPContainmentAdapter.

Uses asyncio.run() wrappers since pytest-asyncio is not available.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Optional

import pytest

from veronica_core.adapters.mcp import MCPContainmentAdapter, MCPToolResult, MCPToolStats
from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter
from veronica_core.circuit_breaker import CircuitBreaker, CircuitState
from veronica_core.containment.execution_context import ExecutionConfig, ExecutionContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(max_cost_usd: float = 100.0, max_steps: int = 200) -> ExecutionContext:
    config = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=max_steps,
        max_retries_total=10,
    )
    return ExecutionContext(config=config)


def _make_sync_adapter(
    max_cost_usd: float = 100.0,
    max_steps: int = 200,
    circuit_breaker: Optional[CircuitBreaker] = None,
    timeout_seconds: Optional[float] = None,
    failure_predicate=None,
    default_cost_per_call: float = 0.001,
) -> MCPContainmentAdapter:
    ctx = _make_ctx(max_cost_usd=max_cost_usd, max_steps=max_steps)
    return MCPContainmentAdapter(
        execution_context=ctx,
        circuit_breaker=circuit_breaker,
        timeout_seconds=timeout_seconds,
        failure_predicate=failure_predicate,
        default_cost_per_call=default_cost_per_call,
    )


def _make_async_adapter(
    max_cost_usd: float = 100.0,
    max_steps: int = 200,
    circuit_breaker: Optional[CircuitBreaker] = None,
    timeout_seconds: Optional[float] = None,
    failure_predicate=None,
    default_cost_per_call: float = 0.001,
) -> AsyncMCPContainmentAdapter:
    ctx = _make_ctx(max_cost_usd=max_cost_usd, max_steps=max_steps)
    return AsyncMCPContainmentAdapter(
        execution_context=ctx,
        circuit_breaker=circuit_breaker,
        timeout_seconds=timeout_seconds,
        failure_predicate=failure_predicate,
        default_cost_per_call=default_cost_per_call,
    )


async def _async_echo(**kwargs: Any) -> dict[str, Any]:
    return {"echo": kwargs}


async def _async_raise(**kwargs: Any) -> Any:
    raise RuntimeError("async tool exploded")


def _sync_echo(**kwargs: Any) -> dict[str, Any]:
    return {"echo": kwargs}


def _sync_raise(**kwargs: Any) -> Any:
    raise RuntimeError("sync tool exploded")


# ---------------------------------------------------------------------------
# Async adapter adversarial tests
# ---------------------------------------------------------------------------


class TestAdversarialAsyncSyncGuard:
    """Passing a sync (non-async) callable to AsyncMCPContainmentAdapter.

    Note: AsyncMCPContainmentAdapter does not add an explicit sync-guard check.
    When a sync fn is passed, the adapter attempts to await the return value,
    which raises a TypeError internally. The adapter catches this as a regular
    Exception and returns success=False with the TypeError message.
    """

    def test_sync_fn_passed_to_async_adapter_returns_failure(self) -> None:
        """Non-async callable results in success=False (TypeError caught internally)."""
        async def run() -> MCPToolResult:
            adapter = _make_async_adapter()
            return await adapter.wrap_tool_call("tool", {}, _sync_echo)  # type: ignore[arg-type]

        result = asyncio.run(run())
        assert result.success is False
        assert result.error is not None
        assert "TypeError" in result.error

    def test_sync_fn_error_has_allow_decision(self) -> None:
        """TypeError from sync fn is a tool-level error -- decision must be ALLOW."""
        async def run() -> MCPToolResult:
            adapter = _make_async_adapter()
            return await adapter.wrap_tool_call("tool", {}, _sync_echo)  # type: ignore[arg-type]

        result = asyncio.run(run())
        assert result.decision == "ALLOW"

    def test_lambda_passed_to_async_adapter_returns_failure(self) -> None:
        """A plain lambda is not a coroutine -- results in success=False."""
        async def run() -> MCPToolResult:
            adapter = _make_async_adapter()
            return await adapter.wrap_tool_call("tool", {}, lambda **k: "value")  # type: ignore[arg-type]

        result = asyncio.run(run())
        assert result.success is False


class TestAdversarialAsyncConcurrency:
    """10 concurrent asyncio.gather calls -- no data races in stats."""

    def test_concurrent_async_calls_no_race(self) -> None:
        """10 concurrent calls must all complete without corruption."""
        async def run() -> dict[str, MCPToolStats]:
            adapter = _make_async_adapter(max_cost_usd=100.0, max_steps=500)
            results = await asyncio.gather(
                *[adapter.wrap_tool_call("tool", {"i": i}, _async_echo) for i in range(10)]
            )
            assert len(results) == 10
            return await adapter.get_tool_stats()

        stats = asyncio.run(run())
        assert stats["tool"].call_count == 10

    def test_concurrent_async_calls_stats_consistent(self) -> None:
        """call_count must be consistent (error_count <= call_count)."""
        async def run() -> MCPToolStats:
            adapter = _make_async_adapter(max_cost_usd=100.0, max_steps=500)
            tasks = []
            for i in range(10):
                fn = _async_raise if i % 2 == 0 else _async_echo
                tasks.append(adapter.wrap_tool_call("tool", {}, fn))
            await asyncio.gather(*tasks)
            stats = await adapter.get_tool_stats()
            return stats["tool"]

        s = asyncio.run(run())
        assert s.error_count >= 0
        assert s.call_count >= s.error_count


class TestAdversarialAsyncTimeout:
    """Timeout edge cases for AsyncMCPContainmentAdapter."""

    def test_timeout_exactly_at_boundary(self) -> None:
        """Call that takes longer than timeout must be recorded as error."""
        async def slightly_slow(**kwargs: Any) -> str:
            await asyncio.sleep(0.2)
            return "done"

        async def run() -> MCPToolResult:
            adapter = _make_async_adapter(timeout_seconds=0.05)
            return await adapter.wrap_tool_call("tool", {}, slightly_slow)

        result = asyncio.run(run())
        assert result.success is False
        assert result.error is not None

    def test_call_fn_raises_after_timeout(self) -> None:
        """call_fn that sleeps beyond timeout is cancelled via asyncio.wait_for."""
        async def very_slow(**kwargs: Any) -> str:
            await asyncio.sleep(10.0)
            return "never"

        async def run() -> MCPToolResult:
            adapter = _make_async_adapter(timeout_seconds=0.05)
            return await adapter.wrap_tool_call("tool", {}, very_slow)

        result = asyncio.run(run())
        assert result.success is False

    def test_timeout_increments_error_count_async(self) -> None:
        """Timeout must increment error_count in stats."""
        async def slow_fn(**kwargs: Any) -> str:
            await asyncio.sleep(0.2)
            return "done"

        async def run() -> MCPToolStats:
            adapter = _make_async_adapter(timeout_seconds=0.05)
            await adapter.wrap_tool_call("tool", {}, slow_fn)
            stats = await adapter.get_tool_stats()
            return stats["tool"]

        s = asyncio.run(run())
        assert s.error_count == 1

    def test_fast_fn_under_timeout_succeeds(self) -> None:
        """Call well within timeout must succeed normally."""
        async def run() -> MCPToolResult:
            adapter = _make_async_adapter(timeout_seconds=5.0)
            return await adapter.wrap_tool_call("tool", {}, _async_echo)

        result = asyncio.run(run())
        assert result.success is True


class TestAdversarialAsyncIsError:
    """isError flag in async adapter results."""

    def test_is_error_with_corrupted_content(self) -> None:
        """isError=True with garbage content must be handled without crash."""
        class CorruptedResult:
            isError = True
            content = b"\x00\xff\xfe"  # binary garbage

        async def corrupt_fn(**kwargs: Any) -> CorruptedResult:
            return CorruptedResult()

        async def run() -> MCPToolResult:
            adapter = _make_async_adapter()
            return await adapter.wrap_tool_call("tool", {}, corrupt_fn)

        result = asyncio.run(run())
        assert result.success is False
        assert result.result is not None

    def test_is_error_true_does_not_trip_cb_async(self) -> None:
        """isError=True must not trip circuit breaker in async adapter."""
        class ErrorResult:
            isError = True

        async def error_fn(**kwargs: Any) -> ErrorResult:
            return ErrorResult()

        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
            adapter = _make_async_adapter(circuit_breaker=cb)
            await adapter.wrap_tool_call("tool", {}, error_fn)
            await adapter.wrap_tool_call("tool", {}, error_fn)
            return cb.state

        state = asyncio.run(run())
        assert state == CircuitState.CLOSED

    def test_is_error_false_success_async(self) -> None:
        """isError=False must result in success=True."""
        class OkResult:
            isError = False

        async def ok_fn(**kwargs: Any) -> OkResult:
            return OkResult()

        async def run() -> MCPToolResult:
            adapter = _make_async_adapter()
            return await adapter.wrap_tool_call("tool", {}, ok_fn)

        result = asyncio.run(run())
        assert result.success is True


class TestAdversarialAsyncFailurePredicate:
    """failure_predicate restricts CB tripping in async adapter."""

    def test_failure_predicate_blocks_cb_trip_async(self) -> None:
        """Predicate returning False must prevent CB from tripping."""
        def never_trip(exc: BaseException) -> bool:
            return False

        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
            adapter = _make_async_adapter(circuit_breaker=cb, failure_predicate=never_trip)
            await adapter.wrap_tool_call("tool", {}, _async_raise)
            await adapter.wrap_tool_call("tool", {}, _async_raise)
            return cb.state

        state = asyncio.run(run())
        assert state == CircuitState.CLOSED

    def test_failure_predicate_selective_async(self) -> None:
        """Only matching exception types should trip CB."""
        def only_os_errors(exc: BaseException) -> bool:
            return isinstance(exc, OSError)

        async def runtime_error_fn(**kwargs: Any) -> Any:
            raise RuntimeError("not an OS error")

        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
            adapter = _make_async_adapter(circuit_breaker=cb, failure_predicate=only_os_errors)
            await adapter.wrap_tool_call("tool", {}, runtime_error_fn)
            await adapter.wrap_tool_call("tool", {}, runtime_error_fn)
            return cb.state

        state = asyncio.run(run())
        assert state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Sync adapter adversarial tests
# ---------------------------------------------------------------------------


class TestAdversarialSyncAsyncGuard:
    """Async callable passed to sync MCPContainmentAdapter."""

    def test_async_fn_raises_type_error(self) -> None:
        """Async callable must raise TypeError immediately."""
        adapter = _make_sync_adapter()
        with pytest.raises(TypeError, match="coroutine function"):
            adapter.wrap_tool_call("tool", {}, _async_echo)  # type: ignore[arg-type]

    def test_async_lambda_detected(self) -> None:
        """An async lambda expression must be detected as coroutine function."""
        async def async_lambda(**kwargs: Any) -> str:
            return "async lambda"

        adapter = _make_sync_adapter()
        with pytest.raises(TypeError):
            adapter.wrap_tool_call("tool", {}, async_lambda)

    def test_async_guard_does_not_touch_budget(self) -> None:
        """TypeError must be raised before budget is consumed."""
        ctx = _make_ctx(max_cost_usd=0.0001)
        adapter = MCPContainmentAdapter(execution_context=ctx, default_cost_per_call=0.0)
        with pytest.raises(TypeError):
            adapter.wrap_tool_call("tool", {}, _async_echo)  # type: ignore[arg-type]
        # Budget should be untouched -- a sync call should still succeed
        result = adapter.wrap_tool_call("tool", {}, _sync_echo)
        assert result.success is True


class TestAdversarialSyncTimeout:
    """Timeout edge cases for sync MCPContainmentAdapter."""

    def test_timeout_zero_immediately_expires(self) -> None:
        """timeout_seconds=0 should expire for any non-zero-duration call."""
        def any_fn(**kwargs: Any) -> str:
            time.sleep(0.05)
            return "done"

        adapter = _make_sync_adapter(timeout_seconds=0.0)
        result = adapter.wrap_tool_call("tool", {}, any_fn)
        # 0s timeout: elapsed > 0 * 1000 = True -> timeout
        assert result.success is False

    def test_timeout_error_message_contains_info(self) -> None:
        """Timeout error result must have a non-None error message."""
        def slow_fn(**kwargs: Any) -> str:
            time.sleep(0.2)
            return "done"

        adapter = _make_sync_adapter(timeout_seconds=0.05)
        result = adapter.wrap_tool_call("tool", {}, slow_fn)
        assert result.success is False
        assert result.error is not None

    def test_concurrent_timeout_calls_five_threads(self) -> None:
        """5 threads with slow fn and short timeout -- must not crash or deadlock."""
        def slow_fn(**kwargs: Any) -> str:
            time.sleep(0.2)
            return "done"

        adapter = _make_sync_adapter(timeout_seconds=0.05)
        results: list[MCPToolResult] = []
        lock = threading.Lock()

        def worker() -> None:
            r = adapter.wrap_tool_call("tool", {}, slow_fn)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 5
        for r in results:
            assert r.success is False

    def test_timeout_with_predicate_does_not_trip_cb(self) -> None:
        """TimeoutError filtered by predicate must not trip CB."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

        def no_timeout_errors(exc: BaseException) -> bool:
            return not isinstance(exc, TimeoutError)

        def slow_fn(**kwargs: Any) -> str:
            time.sleep(0.2)
            return "done"

        adapter = _make_sync_adapter(
            circuit_breaker=cb,
            timeout_seconds=0.05,
            failure_predicate=no_timeout_errors,
        )
        adapter.wrap_tool_call("tool", {}, slow_fn)
        adapter.wrap_tool_call("tool", {}, slow_fn)
        assert cb.state == CircuitState.CLOSED


class TestAdversarialSyncFailurePredicate:
    """failure_predicate selective CB tripping in sync adapter."""

    def test_failure_predicate_with_base_exception_subclass_not_caught(self) -> None:
        """BaseException (non-Exception) subclasses bypass the except clause.

        The sync adapter's _execute() catches only Exception. A custom
        BaseException subclass that is not an Exception subclass is NOT caught
        by the adapter. ExecutionContext.wrap_tool_call may absorb it, resulting
        in success=True with result=None rather than propagating.

        This test documents the observed behavior: predicate is not invoked
        and CB is not tripped for BaseException-only subclasses.
        """
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        predicate_called = [False]

        def predicate(exc: BaseException) -> bool:
            predicate_called[0] = True
            return True

        class CustomBaseError(BaseException):
            pass

        def base_exception_fn(**kwargs: Any) -> Any:
            raise CustomBaseError("base error")

        adapter = _make_sync_adapter(circuit_breaker=cb, failure_predicate=predicate)
        # BaseException subclass is absorbed by ExecutionContext -- no raise
        result = adapter.wrap_tool_call("tool", {}, base_exception_fn)
        # The predicate was not called and CB was not tripped
        assert predicate_called[0] is False
        assert cb.state == CircuitState.CLOSED
        # result may be success=True (absorbed) or success=False depending on
        # ExecutionContext internals; what matters is CB stays CLOSED
        assert result.decision in ("ALLOW", "HALT")

    def test_failure_predicate_exception_type_filtering(self) -> None:
        """Only ValueError should trip CB; RuntimeError should not."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

        def only_value_errors(exc: BaseException) -> bool:
            return isinstance(exc, ValueError)

        adapter = _make_sync_adapter(circuit_breaker=cb, failure_predicate=only_value_errors)
        adapter.wrap_tool_call("tool", {}, _sync_raise)  # RuntimeError
        adapter.wrap_tool_call("tool", {}, _sync_raise)  # RuntimeError
        assert cb.state == CircuitState.CLOSED

    def test_failure_predicate_value_error_trips_cb(self) -> None:
        """ValueError matching the predicate should trip the CB."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

        def only_value_errors(exc: BaseException) -> bool:
            return isinstance(exc, ValueError)

        def value_error_fn(**kwargs: Any) -> Any:
            raise ValueError("bad input")

        adapter = _make_sync_adapter(circuit_breaker=cb, failure_predicate=only_value_errors)
        adapter.wrap_tool_call("tool", {}, value_error_fn)
        adapter.wrap_tool_call("tool", {}, value_error_fn)
        assert cb.state == CircuitState.OPEN


class TestAdversarialSyncIsError:
    """isError=False must not cause failure in sync adapter."""

    def test_is_error_false_does_not_mark_failure(self) -> None:
        """isError=False attribute must be treated as success=True."""
        class OkResult:
            isError = False
            content = "success content"

        def ok_fn(**kwargs: Any) -> OkResult:
            return OkResult()

        adapter = _make_sync_adapter()
        result = adapter.wrap_tool_call("tool", {}, ok_fn)
        assert result.success is True

    def test_is_error_missing_does_not_mark_failure(self) -> None:
        """Result without isError attribute must still be success=True."""
        adapter = _make_sync_adapter()
        result = adapter.wrap_tool_call("tool", {}, _sync_echo)
        assert result.success is True

    def test_is_error_true_result_accessible(self) -> None:
        """Raw result must be accessible even on isError=True."""
        class ErrorResult:
            isError = True
            detail = "tool-level failure"

        obj = ErrorResult()

        def error_fn(**kwargs: Any) -> ErrorResult:
            return obj

        adapter = _make_sync_adapter()
        result = adapter.wrap_tool_call("tool", {}, error_fn)
        assert result.result is obj
        assert result.success is False


# ---------------------------------------------------------------------------
# Cross-adapter: MCPToolStats format must be identical
# ---------------------------------------------------------------------------


class TestAdversarialCrossAdapter:
    """Stats from sync and async adapters must have the same fields."""

    def test_sync_and_async_stats_format_identical(self) -> None:
        """MCPToolStats from both adapters must expose the same attributes."""
        async def run() -> tuple[MCPToolStats, MCPToolStats]:
            sync_adapter = _make_sync_adapter()
            async_adapter = _make_async_adapter()
            sync_adapter.wrap_tool_call("tool", {}, _sync_echo)
            await async_adapter.wrap_tool_call("tool", {}, _async_echo)
            sync_stats = sync_adapter.get_tool_stats()["tool"]
            async_stats = (await async_adapter.get_tool_stats())["tool"]
            return sync_stats, async_stats

        sync_stats, async_stats = asyncio.run(run())

        assert isinstance(sync_stats, MCPToolStats)
        assert isinstance(async_stats, MCPToolStats)

        sync_fields = {f for f in vars(sync_stats) if not f.startswith("_")}
        async_fields = {f for f in vars(async_stats) if not f.startswith("_")}
        assert sync_fields == async_fields

    def test_sync_and_async_both_track_error_count(self) -> None:
        """Both adapters must increment error_count on tool failure."""
        async def run() -> tuple[MCPToolStats, MCPToolStats]:
            sync_adapter = _make_sync_adapter()
            async_adapter = _make_async_adapter()
            sync_adapter.wrap_tool_call("tool", {}, _sync_raise)
            await async_adapter.wrap_tool_call("tool", {}, _async_raise)
            sync_stats = sync_adapter.get_tool_stats()["tool"]
            async_stats = (await async_adapter.get_tool_stats())["tool"]
            return sync_stats, async_stats

        sync_stats, async_stats = asyncio.run(run())
        assert sync_stats.error_count == 1
        assert async_stats.error_count == 1

    def test_sync_and_async_decision_allow_on_tool_error(self) -> None:
        """Both adapters must return decision=ALLOW (not HALT) on tool exception."""
        async def run() -> tuple[MCPToolResult, MCPToolResult]:
            sync_adapter = _make_sync_adapter()
            async_adapter = _make_async_adapter()
            sync_result = sync_adapter.wrap_tool_call("tool", {}, _sync_raise)
            async_result = await async_adapter.wrap_tool_call("tool", {}, _async_raise)
            return sync_result, async_result

        sync_result, async_result = asyncio.run(run())
        assert sync_result.decision == "ALLOW"
        assert async_result.decision == "ALLOW"
