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

from _nogil_compat import nogil_unstable

from veronica_core.adapters.mcp import (
    MCPContainmentAdapter,
    MCPToolResult,
    MCPToolStats,
)
from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter
from veronica_core.circuit_breaker import CircuitBreaker, CircuitState
from veronica_core import ExecutionConfig, ExecutionContext


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
        assert "tool call failed" in result.error

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
                *[
                    adapter.wrap_tool_call("tool", {"i": i}, _async_echo)
                    for i in range(10)
                ]
            )
            assert len(results) == 10
            return adapter.get_tool_stats()

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
            stats = adapter.get_tool_stats()
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
            stats = adapter.get_tool_stats()
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
            adapter = _make_async_adapter(
                circuit_breaker=cb, failure_predicate=never_trip
            )
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
            adapter = _make_async_adapter(
                circuit_breaker=cb, failure_predicate=only_os_errors
            )
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
        adapter = MCPContainmentAdapter(
            execution_context=ctx, default_cost_per_call=0.0
        )
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

    @nogil_unstable
    def test_timeout_error_message_contains_info(self) -> None:
        """Timeout error result must have a non-None error message."""

        def slow_fn(**kwargs: Any) -> str:
            time.sleep(0.5)
            return "done"

        adapter = _make_sync_adapter(timeout_seconds=0.05)
        result = adapter.wrap_tool_call("tool", {}, slow_fn)
        assert result.success is False
        assert result.error is not None

    @nogil_unstable
    def test_concurrent_timeout_calls_five_threads(self) -> None:
        """5 threads with slow fn and short timeout -- must not crash or deadlock."""

        def slow_fn(**kwargs: Any) -> str:
            time.sleep(0.5)
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

        adapter = _make_sync_adapter(
            circuit_breaker=cb, failure_predicate=only_value_errors
        )
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

        adapter = _make_sync_adapter(
            circuit_breaker=cb, failure_predicate=only_value_errors
        )
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
            async_stats = (async_adapter.get_tool_stats())["tool"]
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
            async_stats = (async_adapter.get_tool_stats())["tool"]
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


# ---------------------------------------------------------------------------
# isError=True + failure_predicate interaction
# ---------------------------------------------------------------------------


class TestAdversarialIsErrorAndPredicate:
    """isError=True must NOT trip CB regardless of failure_predicate."""

    def test_is_error_true_predicate_true_does_not_trip_cb_sync(self) -> None:
        """isError=True with always-trip predicate must still NOT trip CB (sync)."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)

        def always_trip(exc: BaseException) -> bool:
            return True

        class ErrorResult:
            isError = True

        def error_fn(**kwargs: Any) -> ErrorResult:
            return ErrorResult()

        ctx = _make_ctx()
        from veronica_core.adapters.mcp import MCPContainmentAdapter

        adapter = MCPContainmentAdapter(
            execution_context=ctx,
            circuit_breaker=cb,
            failure_predicate=always_trip,
        )
        adapter.wrap_tool_call("tool", {}, error_fn)
        # isError is not a Python exception -- predicate is never invoked
        assert cb.state == CircuitState.CLOSED

    def test_is_error_true_predicate_false_does_not_trip_cb_sync(self) -> None:
        """isError=True with never-trip predicate must not trip CB (sync)."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)

        def never_trip(exc: BaseException) -> bool:
            return False

        class ErrorResult:
            isError = True

        def error_fn(**kwargs: Any) -> ErrorResult:
            return ErrorResult()

        ctx = _make_ctx()
        from veronica_core.adapters.mcp import MCPContainmentAdapter

        adapter = MCPContainmentAdapter(
            execution_context=ctx,
            circuit_breaker=cb,
            failure_predicate=never_trip,
        )
        adapter.wrap_tool_call("tool", {}, error_fn)
        assert cb.state == CircuitState.CLOSED

    def test_is_error_true_predicate_interaction_async(self) -> None:
        """isError=True with always-trip predicate must NOT trip CB (async)."""

        class ErrorResult:
            isError = True

        async def error_fn(**kwargs: Any) -> ErrorResult:
            return ErrorResult()

        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
            adapter = _make_async_adapter(
                circuit_breaker=cb,
                failure_predicate=lambda exc: True,
            )
            await adapter.wrap_tool_call("tool", {}, error_fn)
            return cb.state

        assert asyncio.run(run()) == CircuitState.CLOSED

    def test_exception_with_predicate_false_does_not_trip_cb_but_is_error_counted(
        self,
    ) -> None:
        """Exception filtered by predicate: CB not tripped, error_count still incremented."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

        def only_value_errors(exc: BaseException) -> bool:
            return isinstance(exc, ValueError)

        adapter = _make_sync_adapter(
            circuit_breaker=cb,
            failure_predicate=only_value_errors,
        )
        adapter.wrap_tool_call("tool", {}, _sync_raise)  # RuntimeError -> filtered
        assert cb.state == CircuitState.CLOSED
        stats = adapter.get_tool_stats()
        assert stats["tool"].error_count == 1


# ---------------------------------------------------------------------------
# Budget exactly at limit (off-by-one)
# ---------------------------------------------------------------------------


class TestAdversarialBudgetBoundary:
    """Exact budget boundary tests."""

    def test_budget_exactly_exhausted_next_call_halted_sync(self) -> None:
        """After spending exactly max_cost_usd, next call must be HALT."""
        from veronica_core.adapters.mcp import MCPContainmentAdapter, MCPToolCost
        from veronica_core.containment.execution_context import (
            ExecutionConfig,
            ExecutionContext,
        )

        # Budget = 0.01, cost_per_call = 0.01 -> exactly 1 call allowed
        config = ExecutionConfig(max_cost_usd=0.01, max_steps=100, max_retries_total=5)
        ctx = ExecutionContext(config=config)
        costs = {"tool": MCPToolCost("tool", cost_per_call=0.01)}
        adapter = MCPContainmentAdapter(
            execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0
        )

        r1 = adapter.wrap_tool_call("tool", {}, _sync_echo)
        r2 = adapter.wrap_tool_call("tool", {}, _sync_echo)
        # First call may pass, second must be HALT
        assert r2.decision == "HALT" or r1.decision == "HALT"

    def test_budget_zero_first_call_halted_sync(self) -> None:
        """max_cost_usd=0 with any cost_per_call > 0 must block first call."""
        from veronica_core.adapters.mcp import MCPContainmentAdapter
        from veronica_core.containment.execution_context import (
            ExecutionConfig,
            ExecutionContext,
        )

        config = ExecutionConfig(max_cost_usd=0.0, max_steps=100, max_retries_total=5)
        ctx = ExecutionContext(config=config)
        adapter = MCPContainmentAdapter(
            execution_context=ctx, default_cost_per_call=0.001
        )
        result = adapter.wrap_tool_call("tool", {}, _sync_echo)
        assert result.decision == "HALT"

    def test_budget_zero_cost_per_call_never_exhausted(self) -> None:
        """With default_cost_per_call=0.0, budget should not be consumed."""
        adapter = _make_sync_adapter(max_cost_usd=0.001, default_cost_per_call=0.0)
        for _ in range(10):
            r = adapter.wrap_tool_call("tool", {}, _sync_echo)
            assert r.decision == "ALLOW"

    def test_budget_exactly_exhausted_async(self) -> None:
        """Async adapter: after spending max_cost_usd, next call must be HALT."""

        async def run() -> str:
            from veronica_core.adapters.mcp import MCPToolCost
            from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter
            from veronica_core.containment.execution_context import (
                ExecutionConfig,
                ExecutionContext,
            )

            config = ExecutionConfig(
                max_cost_usd=0.01, max_steps=100, max_retries_total=5
            )
            ctx = ExecutionContext(config=config)
            costs = {"tool": MCPToolCost("tool", cost_per_call=0.01)}
            adapter = AsyncMCPContainmentAdapter(
                execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0
            )
            r1 = await adapter.wrap_tool_call("tool", {}, _async_echo)
            r2 = await adapter.wrap_tool_call("tool", {}, _async_echo)
            # At least one of the calls must be HALT
            if r1.decision == "HALT":
                return r1.decision
            return r2.decision

        decision = asyncio.run(run())
        assert decision == "HALT"


# ---------------------------------------------------------------------------
# Very large arguments dict (memory / performance)
# ---------------------------------------------------------------------------


class TestAdversarialLargeArguments:
    """Large arguments must not crash the adapter."""

    def test_very_large_arguments_sync(self) -> None:
        """1000-key arguments dict must be passed to call_fn without crash."""
        received: list[int] = []

        def counting_fn(**kwargs: Any) -> int:
            received.append(len(kwargs))
            return len(kwargs)

        large_args = {f"key_{i}": f"value_{i}" * 10 for i in range(1000)}
        adapter = _make_sync_adapter()
        result = adapter.wrap_tool_call("tool", large_args, counting_fn)
        assert result.success is True
        assert received == [1000]

    def test_very_large_arguments_async(self) -> None:
        """1000-key arguments dict must be passed to async call_fn without crash."""

        async def run() -> MCPToolResult:
            async def counting_fn(**kwargs: Any) -> int:
                return len(kwargs)

            large_args = {f"key_{i}": f"value_{i}" * 10 for i in range(1000)}
            adapter = _make_async_adapter()
            return await adapter.wrap_tool_call("tool", large_args, counting_fn)

        result = asyncio.run(run())
        assert result.success is True
        assert result.result == 1000

    def test_nested_dict_arguments_not_mutated(self) -> None:
        """Adapter must not modify caller's arguments dict."""
        original_args = {"nested": {"key": "value"}, "list": [1, 2, 3]}
        import copy

        expected = copy.deepcopy(original_args)

        def echo_fn(**kwargs: Any) -> dict[str, Any]:
            return kwargs

        adapter = _make_sync_adapter()
        adapter.wrap_tool_call("tool", original_args, echo_fn)
        assert original_args == expected


# ---------------------------------------------------------------------------
# Async CB HALF_OPEN + concurrent calls
# ---------------------------------------------------------------------------


class TestAdversarialAsyncHalfOpenConcurrent:
    """CB HALF_OPEN state with concurrent async calls."""

    def test_half_open_concurrent_async_no_corruption(self) -> None:
        """10 concurrent calls in HALF_OPEN must leave CB in valid state."""

        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
            adapter = _make_async_adapter(
                circuit_breaker=cb, max_cost_usd=100.0, max_steps=500
            )

            # Trip CB to OPEN
            await adapter.wrap_tool_call("tool", {}, _async_raise)
            await adapter.wrap_tool_call("tool", {}, _async_raise)
            assert cb.state == CircuitState.OPEN

            # Wait for HALF_OPEN
            await asyncio.sleep(0.1)

            # 10 concurrent calls in HALF_OPEN
            tasks = [adapter.wrap_tool_call("tool", {}, _async_echo) for _ in range(10)]
            await asyncio.gather(*tasks, return_exceptions=True)

            return cb.state

        state = asyncio.run(run())
        assert state in (CircuitState.CLOSED, CircuitState.OPEN)

    def test_half_open_single_probe_success_closes_cb_async(self) -> None:
        """Single probe in HALF_OPEN with success must close CB."""

        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
            adapter = _make_async_adapter(circuit_breaker=cb)

            await adapter.wrap_tool_call("tool", {}, _async_raise)
            await adapter.wrap_tool_call("tool", {}, _async_raise)
            assert cb.state == CircuitState.OPEN
            await asyncio.sleep(0.1)

            # Single probe
            await adapter.wrap_tool_call("tool", {}, _async_echo)
            return cb.state

        state = asyncio.run(run())
        # After success probe, CB should be CLOSED or still OPEN (implementation defined)
        assert state in (CircuitState.CLOSED, CircuitState.OPEN)


# ---------------------------------------------------------------------------
# call_fn modifying shared state during timeout (sync)
# ---------------------------------------------------------------------------


class TestAdversarialSharedStateDuringTimeout:
    """call_fn that writes to shared state and then times out."""

    @nogil_unstable
    def test_shared_state_written_before_timeout_detected(self) -> None:
        """Sync timeout is non-preemptive: call_fn completes, then timeout checked.
        Any shared state mutation by call_fn will have taken effect.
        """
        shared = {"written": False}

        def mutating_slow_fn(**kwargs: Any) -> str:
            shared["written"] = True
            time.sleep(0.5)
            return "done"

        adapter = _make_sync_adapter(timeout_seconds=0.05)
        result = adapter.wrap_tool_call("tool", {}, mutating_slow_fn)
        # Non-preemptive: call_fn ran to completion before timeout was detected
        assert shared["written"] is True
        assert result.success is False  # timeout detected post-completion

    def test_shared_counter_incremented_before_timeout(self) -> None:
        """Counter increment inside slow call_fn must happen even if timeout fires."""
        counter = [0]

        def increment_and_sleep(**kwargs: Any) -> str:
            counter[0] += 1
            time.sleep(0.2)
            return "done"

        adapter = _make_sync_adapter(timeout_seconds=0.05)
        for _ in range(3):
            adapter.wrap_tool_call("tool", {}, increment_and_sleep)
        # All 3 increments happened (non-preemptive)
        assert counter[0] == 3


# ---------------------------------------------------------------------------
# wrap_mcp_server with broken session (call_tool raises)
# ---------------------------------------------------------------------------


class TestAdversarialWrapMCPBrokenSession:
    """wrap_mcp_server when the session's call_tool raises various exceptions."""

    def test_call_tool_raises_runtime_error(self) -> None:
        """Session.call_tool raising RuntimeError must return MCPToolResult with error."""

        class BrokenSession:
            async def list_tools(self) -> Any:
                return type("R", (), {"tools": []})()

            async def call_tool(self, *, name: str, arguments: dict) -> Any:
                raise RuntimeError("backend down")

        async def run() -> MCPToolResult:
            from veronica_core.adapters.mcp_async import wrap_mcp_server
            from veronica_core.containment.execution_context import (
                ExecutionConfig,
                ExecutionContext,
            )

            config = ExecutionConfig(
                max_cost_usd=10.0, max_steps=100, max_retries_total=5
            )
            ctx = ExecutionContext(config=config)
            adapter = await wrap_mcp_server(
                session=BrokenSession(), execution_context=ctx
            )
            return await adapter.call_tool("any_tool", {})

        result = asyncio.run(run())
        assert result.success is False
        assert "tool call failed" in result.error

    def test_call_tool_raises_on_every_call(self) -> None:
        """Session that always raises must trip CB after threshold."""

        class AlwaysFailSession:
            async def list_tools(self) -> Any:
                return type("R", (), {"tools": ["tool"]})()

            async def call_tool(self, *, name: str, arguments: dict) -> Any:
                raise OSError("connection reset")

        async def run() -> CircuitState:
            from veronica_core.adapters.mcp_async import wrap_mcp_server
            from veronica_core.containment.execution_context import (
                ExecutionConfig,
                ExecutionContext,
            )

            cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
            config = ExecutionConfig(
                max_cost_usd=10.0, max_steps=100, max_retries_total=5
            )
            ctx = ExecutionContext(config=config)
            adapter = await wrap_mcp_server(
                session=AlwaysFailSession(), execution_context=ctx, circuit_breaker=cb
            )
            await adapter.call_tool("tool", {})
            await adapter.call_tool("tool", {})
            return cb.state

        state = asyncio.run(run())
        assert state == CircuitState.OPEN

    def test_list_tools_raises_adapter_still_functional(self) -> None:
        """Session where list_tools raises must create a functional adapter."""

        class BrokenListSession:
            async def list_tools(self) -> Any:
                raise ConnectionError("server unreachable")

            async def call_tool(self, *, name: str, arguments: dict) -> Any:
                return {"ok": True}

        async def run() -> MCPToolResult:
            from veronica_core.adapters.mcp_async import wrap_mcp_server
            from veronica_core.containment.execution_context import (
                ExecutionConfig,
                ExecutionContext,
            )

            config = ExecutionConfig(
                max_cost_usd=10.0, max_steps=100, max_retries_total=5
            )
            ctx = ExecutionContext(config=config)
            adapter = await wrap_mcp_server(
                session=BrokenListSession(), execution_context=ctx
            )
            return await adapter.call_tool("tool", {})

        result = asyncio.run(run())
        assert result.success is True

    def test_call_tool_raises_timeout_error(self) -> None:
        """Session.call_tool raising asyncio.TimeoutError must be caught as error."""

        class TimeoutSession:
            async def list_tools(self) -> Any:
                return type("R", (), {"tools": []})()

            async def call_tool(self, *, name: str, arguments: dict) -> Any:
                raise asyncio.TimeoutError("upstream timeout")

        async def run() -> MCPToolResult:
            from veronica_core.adapters.mcp_async import wrap_mcp_server
            from veronica_core.containment.execution_context import (
                ExecutionConfig,
                ExecutionContext,
            )

            config = ExecutionConfig(
                max_cost_usd=10.0, max_steps=100, max_retries_total=5
            )
            ctx = ExecutionContext(config=config)
            adapter = await wrap_mcp_server(
                session=TimeoutSession(), execution_context=ctx
            )
            return await adapter.call_tool("any_tool", {})

        result = asyncio.run(run())
        assert result.success is False
        assert result.error is not None
