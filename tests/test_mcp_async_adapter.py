"""Tests for veronica_core.adapters.mcp_async.AsyncMCPContainmentAdapter.

Uses asyncio.run() wrappers since pytest-asyncio is not available.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from veronica_core.adapters.mcp import MCPToolCost, MCPToolResult, MCPToolStats
from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter
from veronica_core.circuit_breaker import CircuitBreaker, CircuitState
from veronica_core.containment.execution_context import (
    ExecutionConfig,
    ExecutionContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(max_cost_usd: float = 10.0, max_steps: int = 100) -> ExecutionContext:
    config = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=max_steps,
        max_retries_total=5,
    )
    return ExecutionContext(config=config)


def _make_adapter(
    max_cost_usd: float = 10.0,
    max_steps: int = 100,
    tool_costs: Optional[dict[str, MCPToolCost]] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    default_cost_per_call: float = 0.001,
    timeout_seconds: Optional[float] = None,
    failure_predicate=None,
) -> AsyncMCPContainmentAdapter:
    ctx = _make_ctx(max_cost_usd=max_cost_usd, max_steps=max_steps)
    return AsyncMCPContainmentAdapter(
        execution_context=ctx,
        tool_costs=tool_costs,
        circuit_breaker=circuit_breaker,
        default_cost_per_call=default_cost_per_call,
        timeout_seconds=timeout_seconds,
        failure_predicate=failure_predicate,
    )


async def _echo_fn(**kwargs: Any) -> dict[str, Any]:
    return {"echo": kwargs}


async def _raise_fn(**kwargs: Any) -> Any:
    raise RuntimeError("tool exploded")


async def _return_none_fn(**kwargs: Any) -> None:
    return None


async def _slow_fn(**kwargs: Any) -> str:
    await asyncio.sleep(10.0)
    return "done"


# ---------------------------------------------------------------------------
# Basic success / failure
# ---------------------------------------------------------------------------


class TestBasicSuccess:
    def test_returns_mcp_tool_result(self) -> None:
        async def run() -> MCPToolResult:
            adapter = _make_adapter()
            return await adapter.wrap_tool_call("search", {"query": "hello"}, _echo_fn)

        result = asyncio.run(run())
        assert isinstance(result, MCPToolResult)

    def test_success_flag_true(self) -> None:
        async def run() -> bool:
            adapter = _make_adapter()
            r = await adapter.wrap_tool_call("search", {}, _echo_fn)
            return r.success

        assert asyncio.run(run()) is True

    def test_decision_allow(self) -> None:
        async def run() -> str:
            adapter = _make_adapter()
            r = await adapter.wrap_tool_call("search", {}, _echo_fn)
            return r.decision

        assert asyncio.run(run()) == "ALLOW"

    def test_result_passed_through(self) -> None:
        async def run() -> Any:
            adapter = _make_adapter()
            r = await adapter.wrap_tool_call("search", {"q": "test"}, _echo_fn)
            return r.result

        assert asyncio.run(run()) == {"echo": {"q": "test"}}

    def test_default_cost_recorded(self) -> None:
        async def run() -> float:
            adapter = _make_adapter(default_cost_per_call=0.005)
            r = await adapter.wrap_tool_call("search", {}, _echo_fn)
            return r.cost_usd

        assert asyncio.run(run()) == pytest.approx(0.005)

    def test_custom_cost_recorded(self) -> None:
        async def run() -> float:
            costs = {"web_search": MCPToolCost("web_search", cost_per_call=0.01)}
            adapter = _make_adapter(tool_costs=costs)
            r = await adapter.wrap_tool_call("web_search", {}, _echo_fn)
            return r.cost_usd

        assert asyncio.run(run()) == pytest.approx(0.01)

    def test_unknown_tool_uses_default(self) -> None:
        async def run() -> float:
            costs = {"known_tool": MCPToolCost("known_tool", cost_per_call=0.05)}
            adapter = _make_adapter(tool_costs=costs, default_cost_per_call=0.002)
            r = await adapter.wrap_tool_call("unknown_tool", {}, _echo_fn)
            return r.cost_usd

        assert asyncio.run(run()) == pytest.approx(0.002)

    def test_call_fn_receives_arguments(self) -> None:
        received: list[dict] = []

        async def capturing_fn(**kwargs: Any) -> None:
            received.append(kwargs)

        async def run() -> None:
            adapter = _make_adapter()
            await adapter.wrap_tool_call(
                "search", {"query": "hello", "limit": 5}, capturing_fn
            )

        asyncio.run(run())
        assert received == [{"query": "hello", "limit": 5}]

    def test_call_fn_returns_none(self) -> None:
        async def run() -> MCPToolResult:
            adapter = _make_adapter()
            return await adapter.wrap_tool_call("search", {}, _return_none_fn)

        result = asyncio.run(run())
        assert result.success is True
        assert result.result is None


class TestBasicFailure:
    def test_failure_success_false(self) -> None:
        async def run() -> MCPToolResult:
            adapter = _make_adapter()
            return await adapter.wrap_tool_call("search", {}, _raise_fn)

        result = asyncio.run(run())
        assert result.success is False

    def test_failure_decision_allow(self) -> None:
        async def run() -> str:
            adapter = _make_adapter()
            r = await adapter.wrap_tool_call("search", {}, _raise_fn)
            return r.decision

        assert asyncio.run(run()) == "ALLOW"

    def test_failure_error_is_generic(self) -> None:
        async def run() -> Optional[str]:
            adapter = _make_adapter()
            r = await adapter.wrap_tool_call("search", {}, _raise_fn)
            return r.error

        error = asyncio.run(run())
        assert error is not None
        assert "tool call failed" in error


# ---------------------------------------------------------------------------
# Budget HALT
# ---------------------------------------------------------------------------


class TestBudgetHalt:
    def test_halt_when_budget_exceeded(self) -> None:
        async def run() -> str:
            adapter = _make_adapter(max_cost_usd=0.0001, default_cost_per_call=0.01)
            await adapter.wrap_tool_call("search", {}, _echo_fn)
            r = await adapter.wrap_tool_call("search", {}, _echo_fn)
            return r.decision

        decision = asyncio.run(run())
        assert decision == "HALT"

    def test_halt_result_success_false(self) -> None:
        async def run() -> MCPToolResult:
            adapter = _make_adapter(max_cost_usd=0.0001, default_cost_per_call=0.01)
            await adapter.wrap_tool_call("search", {}, _echo_fn)
            return await adapter.wrap_tool_call("search", {}, _echo_fn)

        result = asyncio.run(run())
        assert result.decision == "HALT"
        assert result.success is False

    def test_halt_result_has_error_message(self) -> None:
        async def run() -> MCPToolResult:
            adapter = _make_adapter(max_cost_usd=0.0001, default_cost_per_call=0.01)
            await adapter.wrap_tool_call("search", {}, _echo_fn)
            return await adapter.wrap_tool_call("search", {}, _echo_fn)

        result = asyncio.run(run())
        assert result.decision == "HALT"
        assert result.error is not None

    def test_halt_cost_is_zero(self) -> None:
        async def run() -> MCPToolResult:
            adapter = _make_adapter(max_cost_usd=0.0001, default_cost_per_call=0.01)
            await adapter.wrap_tool_call("search", {}, _echo_fn)
            return await adapter.wrap_tool_call("search", {}, _echo_fn)

        result = asyncio.run(run())
        assert result.decision == "HALT"
        assert result.cost_usd == 0.0


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_circuit_open_blocks_call(self) -> None:
        async def run() -> str:
            cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
            adapter = _make_adapter(circuit_breaker=cb)
            for _ in range(3):
                await adapter.wrap_tool_call("search", {}, _raise_fn)
            assert cb.state == CircuitState.OPEN
            r = await adapter.wrap_tool_call("search", {}, _echo_fn)
            return r.decision

        assert asyncio.run(run()) == "HALT"

    def test_circuit_open_halt_error_message(self) -> None:
        async def run() -> MCPToolResult:
            cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
            adapter = _make_adapter(circuit_breaker=cb)
            await adapter.wrap_tool_call("search", {}, _raise_fn)
            await adapter.wrap_tool_call("search", {}, _raise_fn)
            return await adapter.wrap_tool_call("search", {}, _echo_fn)

        result = asyncio.run(run())
        if result.decision == "HALT":
            assert result.error is not None
            assert "circuit" in result.error.lower() or "Circuit" in result.error

    def test_circuit_open_does_not_call_fn(self) -> None:
        call_count = [0]

        async def counting_fn(**kwargs: Any) -> None:
            call_count[0] += 1

        async def run() -> None:
            cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
            adapter = _make_adapter(circuit_breaker=cb)
            await adapter.wrap_tool_call("search", {}, _raise_fn)
            await adapter.wrap_tool_call("search", {}, _raise_fn)
            await adapter.wrap_tool_call("search", {}, counting_fn)

        asyncio.run(run())
        assert call_count[0] == 0

    def test_success_keeps_circuit_closed(self) -> None:
        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
            adapter = _make_adapter(circuit_breaker=cb)
            for _ in range(5):
                await adapter.wrap_tool_call("search", {}, _echo_fn)
            return cb.state

        assert asyncio.run(run()) == CircuitState.CLOSED

    def test_failure_increments_cb_count(self) -> None:
        async def run() -> int:
            cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
            adapter = _make_adapter(circuit_breaker=cb)
            await adapter.wrap_tool_call("search", {}, _raise_fn)
            return cb.failure_count

        assert asyncio.run(run()) == 1


# ---------------------------------------------------------------------------
# Timeout enforcement
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_timeout_raises_and_fails(self) -> None:
        async def run() -> MCPToolResult:
            adapter = _make_adapter(timeout_seconds=0.05)
            return await adapter.wrap_tool_call("slow_tool", {}, _slow_fn)

        result = asyncio.run(run())
        assert result.success is False
        assert result.error is not None
        assert "tool call failed" in result.error

    def test_timeout_does_not_trip_cb_if_predicate_excludes(self) -> None:
        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
            # Predicate: only RuntimeError trips CB (not TimeoutError)
            predicate = lambda exc: isinstance(exc, RuntimeError)  # noqa: E731
            adapter = _make_adapter(
                circuit_breaker=cb,
                timeout_seconds=0.05,
                failure_predicate=predicate,
            )
            await adapter.wrap_tool_call("slow_tool", {}, _slow_fn)
            return cb.state

        # TimeoutError excluded from CB tripping, circuit stays CLOSED
        state = asyncio.run(run())
        assert state == CircuitState.CLOSED

    def test_timeout_trips_cb_without_predicate(self) -> None:
        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
            adapter = _make_adapter(circuit_breaker=cb, timeout_seconds=0.05)
            await adapter.wrap_tool_call("slow_tool", {}, _slow_fn)
            return cb.state

        state = asyncio.run(run())
        assert state == CircuitState.OPEN

    def test_no_timeout_fast_call_succeeds(self) -> None:
        async def run() -> MCPToolResult:
            adapter = _make_adapter(timeout_seconds=5.0)
            return await adapter.wrap_tool_call("search", {}, _echo_fn)

        result = asyncio.run(run())
        assert result.success is True


# ---------------------------------------------------------------------------
# isError detection
# ---------------------------------------------------------------------------


class TestIsErrorDetection:
    def test_is_error_true_returns_failure(self) -> None:
        class ErrorResult:
            isError = True
            content = "something went wrong"

        async def is_error_fn(**kwargs: Any) -> ErrorResult:
            return ErrorResult()

        async def run() -> MCPToolResult:
            adapter = _make_adapter()
            return await adapter.wrap_tool_call("tool", {}, is_error_fn)

        result = asyncio.run(run())
        assert result.success is False
        assert result.decision == "ALLOW"
        assert result.error is not None and "isError" in result.error

    def test_is_error_true_does_not_trip_cb(self) -> None:
        class ErrorResult:
            isError = True

        async def is_error_fn(**kwargs: Any) -> ErrorResult:
            return ErrorResult()

        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
            adapter = _make_adapter(circuit_breaker=cb)
            await adapter.wrap_tool_call("tool", {}, is_error_fn)
            return cb.state

        state = asyncio.run(run())
        assert state == CircuitState.CLOSED

    def test_is_error_false_returns_success(self) -> None:
        class OkResult:
            isError = False
            content = "all good"

        async def ok_fn(**kwargs: Any) -> OkResult:
            return OkResult()

        async def run() -> MCPToolResult:
            adapter = _make_adapter()
            return await adapter.wrap_tool_call("tool", {}, ok_fn)

        result = asyncio.run(run())
        assert result.success is True

    def test_no_is_error_attr_treated_as_success(self) -> None:
        async def run() -> MCPToolResult:
            adapter = _make_adapter()
            return await adapter.wrap_tool_call("tool", {}, _echo_fn)

        result = asyncio.run(run())
        assert result.success is True

    def test_is_error_result_preserved_in_result_field(self) -> None:
        class ErrorResult:
            isError = True

        obj = ErrorResult()

        async def err_fn(**kwargs: Any) -> ErrorResult:
            return obj

        async def run() -> Any:
            adapter = _make_adapter()
            r = await adapter.wrap_tool_call("tool", {}, err_fn)
            return r.result

        assert asyncio.run(run()) is obj


# ---------------------------------------------------------------------------
# failure_predicate filtering
# ---------------------------------------------------------------------------


class TestFailurePredicate:
    def test_predicate_true_trips_cb(self) -> None:
        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
            predicate = lambda exc: isinstance(exc, RuntimeError)  # noqa: E731
            adapter = _make_adapter(circuit_breaker=cb, failure_predicate=predicate)
            await adapter.wrap_tool_call("tool", {}, _raise_fn)
            return cb.state

        assert asyncio.run(run()) == CircuitState.OPEN

    def test_predicate_false_does_not_trip_cb(self) -> None:
        async def value_error_fn(**kwargs: Any) -> Any:
            raise ValueError("filtered out")

        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
            # Only RuntimeError trips CB; ValueError is excluded
            predicate = lambda exc: isinstance(exc, RuntimeError)  # noqa: E731
            adapter = _make_adapter(circuit_breaker=cb, failure_predicate=predicate)
            await adapter.wrap_tool_call("tool", {}, value_error_fn)
            return cb.state

        state = asyncio.run(run())
        assert state == CircuitState.CLOSED

    def test_predicate_false_still_records_error(self) -> None:
        async def value_error_fn(**kwargs: Any) -> Any:
            raise ValueError("filtered")

        async def run() -> MCPToolStats:
            predicate = lambda exc: isinstance(exc, RuntimeError)  # noqa: E731
            adapter = _make_adapter(failure_predicate=predicate)
            await adapter.wrap_tool_call("tool", {}, value_error_fn)
            stats = adapter.get_tool_stats()
            return stats["tool"]

        stats = asyncio.run(run())
        assert stats.error_count == 1

    def test_no_predicate_all_exceptions_trip_cb(self) -> None:
        async def value_error_fn(**kwargs: Any) -> Any:
            raise ValueError("no predicate")

        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
            adapter = _make_adapter(circuit_breaker=cb)
            await adapter.wrap_tool_call("tool", {}, value_error_fn)
            return cb.state

        assert asyncio.run(run()) == CircuitState.OPEN


# ---------------------------------------------------------------------------
# Stats tracking
# ---------------------------------------------------------------------------


class TestStatsTracking:
    def test_call_count_incremented_on_success(self) -> None:
        async def run() -> int:
            adapter = _make_adapter()
            await adapter.wrap_tool_call("search", {}, _echo_fn)
            await adapter.wrap_tool_call("search", {}, _echo_fn)
            stats = adapter.get_tool_stats()
            return stats["search"].call_count

        assert asyncio.run(run()) == 2

    def test_call_count_incremented_on_error(self) -> None:
        async def run() -> int:
            adapter = _make_adapter()
            await adapter.wrap_tool_call("search", {}, _raise_fn)
            stats = adapter.get_tool_stats()
            return stats["search"].call_count

        assert asyncio.run(run()) == 1

    def test_error_count_incremented_on_raise(self) -> None:
        async def run() -> int:
            adapter = _make_adapter()
            await adapter.wrap_tool_call("search", {}, _raise_fn)
            stats = adapter.get_tool_stats()
            return stats["search"].error_count

        assert asyncio.run(run()) == 1

    def test_error_count_not_incremented_on_success(self) -> None:
        async def run() -> int:
            adapter = _make_adapter()
            await adapter.wrap_tool_call("search", {}, _echo_fn)
            stats = adapter.get_tool_stats()
            return stats["search"].error_count

        assert asyncio.run(run()) == 0

    def test_cost_accumulated_on_success(self) -> None:
        async def run() -> float:
            adapter = _make_adapter(default_cost_per_call=0.01)
            await adapter.wrap_tool_call("search", {}, _echo_fn)
            await adapter.wrap_tool_call("search", {}, _echo_fn)
            stats = adapter.get_tool_stats()
            return stats["search"].total_cost_usd

        assert asyncio.run(run()) == pytest.approx(0.02)

    def test_cost_not_accumulated_on_error(self) -> None:
        async def run() -> float:
            adapter = _make_adapter(default_cost_per_call=0.01)
            await adapter.wrap_tool_call("search", {}, _raise_fn)
            stats = adapter.get_tool_stats()
            return stats["search"].total_cost_usd

        assert asyncio.run(run()) == pytest.approx(0.0)

    def test_avg_duration_positive_after_success(self) -> None:
        async def run() -> float:
            adapter = _make_adapter()
            await adapter.wrap_tool_call("search", {}, _echo_fn)
            stats = adapter.get_tool_stats()
            return stats["search"].avg_duration_ms

        assert asyncio.run(run()) >= 0.0

    def test_multiple_tools_tracked_separately(self) -> None:
        async def run() -> tuple[int, int]:
            adapter = _make_adapter()
            await adapter.wrap_tool_call("tool_a", {}, _echo_fn)
            await adapter.wrap_tool_call("tool_b", {}, _echo_fn)
            await adapter.wrap_tool_call("tool_a", {}, _echo_fn)
            stats = adapter.get_tool_stats()
            return stats["tool_a"].call_count, stats["tool_b"].call_count

        a_count, b_count = asyncio.run(run())
        assert a_count == 2
        assert b_count == 1

    def test_get_tool_stats_returns_snapshot(self) -> None:
        async def run() -> tuple[bool, bool]:
            adapter = _make_adapter()
            stats1 = adapter.get_tool_stats()
            await adapter.wrap_tool_call("new_tool", {}, _echo_fn)
            stats2 = adapter.get_tool_stats()
            return "new_tool" not in stats1, "new_tool" in stats2

        not_in_first, in_second = asyncio.run(run())
        assert not_in_first
        assert in_second

    def test_per_token_cost_applied(self) -> None:
        async def token_fn(**kwargs: Any) -> dict[str, Any]:
            return {"token_count": 100}

        async def run() -> float:
            costs = {"llm": MCPToolCost("llm", cost_per_call=0.0, cost_per_token=0.001)}
            adapter = _make_adapter(tool_costs=costs)
            r = await adapter.wrap_tool_call("llm", {}, token_fn)
            return r.cost_usd

        assert asyncio.run(run()) == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_negative_default_cost_raises(self) -> None:
        ctx = _make_ctx()
        with pytest.raises(ValueError, match="default_cost_per_call"):
            AsyncMCPContainmentAdapter(
                execution_context=ctx, default_cost_per_call=-0.1
            )

    def test_zero_default_cost_valid(self) -> None:
        async def run() -> MCPToolResult:
            adapter = _make_adapter(default_cost_per_call=0.0)
            return await adapter.wrap_tool_call("search", {}, _echo_fn)

        result = asyncio.run(run())
        assert result.success is True
        assert result.cost_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Concurrent async calls
# ---------------------------------------------------------------------------


class TestConcurrentAsync:
    def test_concurrent_calls_all_complete(self) -> None:
        async def run() -> list[MCPToolResult]:
            adapter = _make_adapter(max_cost_usd=100.0, max_steps=200)
            tasks = [
                asyncio.create_task(
                    adapter.wrap_tool_call("search", {"i": i}, _echo_fn)
                )
                for i in range(10)
            ]
            return await asyncio.gather(*tasks)

        results = asyncio.run(run())
        assert len(results) == 10

    def test_concurrent_calls_stats_consistent(self) -> None:
        async def run() -> int:
            adapter = _make_adapter(
                max_cost_usd=100.0, max_steps=200, default_cost_per_call=0.0
            )
            tasks = [
                asyncio.create_task(adapter.wrap_tool_call("search", {}, _echo_fn))
                for _ in range(10)
            ]
            await asyncio.gather(*tasks)
            stats = adapter.get_tool_stats()
            return stats["search"].call_count

        count = asyncio.run(run())
        assert count == 10

    def test_concurrent_mixed_success_failure(self) -> None:
        async def run() -> tuple[int, int]:
            adapter = _make_adapter(max_cost_usd=100.0, max_steps=200)
            tasks = []
            for i in range(10):
                fn = _raise_fn if i % 2 == 0 else _echo_fn
                tasks.append(
                    asyncio.create_task(adapter.wrap_tool_call("search", {}, fn))
                )
            results = await asyncio.gather(*tasks)
            successes = sum(1 for r in results if r.success)
            failures = sum(1 for r in results if not r.success)
            return successes, failures

        successes, failures = asyncio.run(run())
        assert successes == 5
        assert failures == 5

    def test_ensure_stats_toctou_no_lost_counts(self) -> None:
        """B-3/S-8 adversarial: 50 coroutines call wrap_tool_call for the SAME
        tool simultaneously. Before the fix, two coroutines could both pass
        the outer `if tool_name not in self._stats` check, and the second
        would silently replace the first's MCPToolStats entry, losing counts.

        After fix, check is inside the lock -- all 50 calls must be counted."""

        async def run() -> int:
            adapter = _make_adapter(
                max_cost_usd=100.0, max_steps=200, default_cost_per_call=0.0
            )
            tasks = [
                asyncio.create_task(adapter.wrap_tool_call("search", {}, _echo_fn))
                for _ in range(50)
            ]
            await asyncio.gather(*tasks)
            stats = adapter.get_tool_stats()
            return stats["search"].call_count

        count = asyncio.run(run())
        assert count == 50, (
            f"Expected 50 calls counted, got {count} (stats entry was replaced)"
        )
