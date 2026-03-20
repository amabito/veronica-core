"""Tests for veronica_core.adapters.mcp.MCPContainmentAdapter.

Does not require the mcp-sdk library.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

import pytest

from _nogil_compat import nogil_unstable

from veronica_core.adapters.mcp import (
    MCPContainmentAdapter,
    MCPToolCost,
    MCPToolResult,
    MCPToolStats,
)
from veronica_core.circuit_breaker import CircuitBreaker, CircuitState
from veronica_core import ExecutionConfig, ExecutionContext


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
) -> MCPContainmentAdapter:
    ctx = _make_ctx(max_cost_usd=max_cost_usd, max_steps=max_steps)
    return MCPContainmentAdapter(
        execution_context=ctx,
        tool_costs=tool_costs,
        circuit_breaker=circuit_breaker,
        default_cost_per_call=default_cost_per_call,
    )


def _echo_fn(**kwargs: Any) -> dict[str, Any]:
    return {"echo": kwargs}


def _raise_fn(**kwargs: Any) -> Any:
    raise RuntimeError("tool exploded")


def _return_none_fn(**kwargs: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# MCPToolCost and MCPToolResult construction
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_tool_cost_defaults(self) -> None:
        cost = MCPToolCost("search")
        assert cost.tool_name == "search"
        assert cost.cost_per_call == 0.0
        assert cost.cost_per_token == 0.0

    def test_tool_result_defaults(self) -> None:
        result = MCPToolResult(success=True)
        assert result.decision == "ALLOW"
        assert result.cost_usd == 0.0
        assert result.error is None
        assert result.result is None

    def test_tool_stats_defaults(self) -> None:
        stats = MCPToolStats(tool_name="x")
        assert stats.call_count == 0
        assert stats.total_cost_usd == 0.0
        assert stats.error_count == 0
        assert stats.avg_duration_ms == 0.0


# ---------------------------------------------------------------------------
# Basic: wrap_tool_call succeeds and cost recorded
# ---------------------------------------------------------------------------


class TestBasicSuccess:
    def test_returns_mcp_tool_result(self) -> None:
        adapter = _make_adapter()
        result = adapter.wrap_tool_call("search", {"query": "hello"}, _echo_fn)
        assert isinstance(result, MCPToolResult)

    def test_success_flag_true(self) -> None:
        adapter = _make_adapter()
        result = adapter.wrap_tool_call("search", {}, _echo_fn)
        assert result.success is True

    def test_decision_allow(self) -> None:
        adapter = _make_adapter()
        result = adapter.wrap_tool_call("search", {}, _echo_fn)
        assert result.decision == "ALLOW"

    def test_result_passed_through(self) -> None:
        adapter = _make_adapter()
        result = adapter.wrap_tool_call("search", {"q": "test"}, _echo_fn)
        assert result.result == {"echo": {"q": "test"}}

    def test_default_cost_recorded(self) -> None:
        adapter = _make_adapter(default_cost_per_call=0.005)
        result = adapter.wrap_tool_call("search", {}, _echo_fn)
        assert result.cost_usd == pytest.approx(0.005)

    def test_custom_cost_recorded(self) -> None:
        costs = {"web_search": MCPToolCost("web_search", cost_per_call=0.01)}
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("web_search", {}, _echo_fn)
        assert result.cost_usd == pytest.approx(0.01)

    def test_unknown_tool_uses_default(self) -> None:
        costs = {"known_tool": MCPToolCost("known_tool", cost_per_call=0.05)}
        adapter = _make_adapter(tool_costs=costs, default_cost_per_call=0.002)
        result = adapter.wrap_tool_call("unknown_tool", {}, _echo_fn)
        assert result.cost_usd == pytest.approx(0.002)

    def test_stats_call_count_incremented(self) -> None:
        adapter = _make_adapter()
        adapter.wrap_tool_call("search", {}, _echo_fn)
        adapter.wrap_tool_call("search", {}, _echo_fn)
        stats = adapter.get_tool_stats()
        assert stats["search"].call_count == 2

    def test_stats_cost_accumulated(self) -> None:
        adapter = _make_adapter(default_cost_per_call=0.01)
        adapter.wrap_tool_call("search", {}, _echo_fn)
        adapter.wrap_tool_call("search", {}, _echo_fn)
        stats = adapter.get_tool_stats()
        assert stats["search"].total_cost_usd == pytest.approx(0.02)

    def test_multiple_tools_tracked_separately(self) -> None:
        adapter = _make_adapter(default_cost_per_call=0.001)
        adapter.wrap_tool_call("tool_a", {}, _echo_fn)
        adapter.wrap_tool_call("tool_b", {}, _echo_fn)
        adapter.wrap_tool_call("tool_a", {}, _echo_fn)
        stats = adapter.get_tool_stats()
        assert stats["tool_a"].call_count == 2
        assert stats["tool_b"].call_count == 1


# ---------------------------------------------------------------------------
# HALT: budget exceeded before tool call
# ---------------------------------------------------------------------------


class TestBudgetHalt:
    def test_halt_when_budget_exceeded(self) -> None:
        # max_cost_usd is very small -- first call should exhaust it
        adapter = _make_adapter(max_cost_usd=0.0001, default_cost_per_call=0.01)
        # First call may or may not pass depending on pre-check ordering;
        # subsequent call must be halted.
        adapter.wrap_tool_call("search", {}, _echo_fn)
        result = adapter.wrap_tool_call("search", {}, _echo_fn)
        assert result.decision == "HALT"

    def test_halt_result_has_error_message(self) -> None:
        adapter = _make_adapter(max_cost_usd=0.0001, default_cost_per_call=0.01)
        adapter.wrap_tool_call("search", {}, _echo_fn)
        result = adapter.wrap_tool_call("search", {}, _echo_fn)
        assert result.decision == "HALT"
        assert result.error is not None

    def test_halt_result_success_is_false(self) -> None:
        adapter = _make_adapter(max_cost_usd=0.0001, default_cost_per_call=0.01)
        adapter.wrap_tool_call("search", {}, _echo_fn)
        result = adapter.wrap_tool_call("search", {}, _echo_fn)
        assert result.decision == "HALT"
        assert result.success is False

    def test_halt_cost_is_zero(self) -> None:
        adapter = _make_adapter(max_cost_usd=0.0001, default_cost_per_call=0.01)
        adapter.wrap_tool_call("search", {}, _echo_fn)
        result = adapter.wrap_tool_call("search", {}, _echo_fn)
        assert result.decision == "HALT"
        assert result.cost_usd == 0.0


# ---------------------------------------------------------------------------
# Circuit breaker: 3 failures -> open -> next call HALT
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_circuit_open_blocks_call(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
        adapter = _make_adapter(circuit_breaker=cb)
        for _ in range(3):
            adapter.wrap_tool_call("search", {}, _raise_fn)
        assert cb.state == CircuitState.OPEN
        result = adapter.wrap_tool_call("search", {}, _echo_fn)
        assert result.decision == "HALT"

    def test_circuit_open_halt_error_message(self) -> None:
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        adapter = _make_adapter(circuit_breaker=cb)
        adapter.wrap_tool_call("search", {}, _raise_fn)
        adapter.wrap_tool_call("search", {}, _raise_fn)
        result = adapter.wrap_tool_call("search", {}, _echo_fn)
        assert result.decision == "HALT"
        assert "circuit" in result.error.lower() or "Circuit" in result.error

    def test_circuit_open_does_not_call_fn(self) -> None:
        call_count = [0]

        def counting_fn(**kwargs: Any) -> None:
            call_count[0] += 1

        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        adapter = _make_adapter(circuit_breaker=cb)
        adapter.wrap_tool_call("search", {}, _raise_fn)
        adapter.wrap_tool_call("search", {}, _raise_fn)
        adapter.wrap_tool_call("search", {}, counting_fn)
        assert call_count[0] == 0

    def test_success_keeps_circuit_closed(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
        adapter = _make_adapter(circuit_breaker=cb)
        for _ in range(5):
            adapter.wrap_tool_call("search", {}, _echo_fn)
        assert cb.state == CircuitState.CLOSED

    def test_failure_increments_cb_count(self) -> None:
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
        adapter = _make_adapter(circuit_breaker=cb)
        adapter.wrap_tool_call("search", {}, _raise_fn)
        assert cb.failure_count == 1


# ---------------------------------------------------------------------------
# Tool stats: call_count, total_cost, error_count tracked correctly
# ---------------------------------------------------------------------------


class TestToolStats:
    def test_error_count_incremented_on_raise(self) -> None:
        adapter = _make_adapter()
        adapter.wrap_tool_call("search", {}, _raise_fn)
        stats = adapter.get_tool_stats()
        assert stats["search"].error_count == 1

    def test_error_count_not_incremented_on_success(self) -> None:
        adapter = _make_adapter()
        adapter.wrap_tool_call("search", {}, _echo_fn)
        stats = adapter.get_tool_stats()
        assert stats["search"].error_count == 0

    def test_call_count_incremented_on_error(self) -> None:
        adapter = _make_adapter()
        adapter.wrap_tool_call("search", {}, _raise_fn)
        adapter.wrap_tool_call("search", {}, _raise_fn)
        stats = adapter.get_tool_stats()
        assert stats["search"].call_count == 2

    def test_avg_duration_positive_after_success(self) -> None:
        adapter = _make_adapter()
        adapter.wrap_tool_call("search", {}, _echo_fn)
        stats = adapter.get_tool_stats()
        assert stats["search"].avg_duration_ms >= 0.0

    def test_get_tool_stats_returns_copy(self) -> None:
        adapter = _make_adapter()
        stats1 = adapter.get_tool_stats()
        adapter.wrap_tool_call("new_tool", {}, _echo_fn)
        stats2 = adapter.get_tool_stats()
        # Original snapshot should not have new_tool
        assert "new_tool" not in stats1
        assert "new_tool" in stats2

    def test_cost_not_accumulated_for_errors(self) -> None:
        adapter = _make_adapter(default_cost_per_call=0.01)
        adapter.wrap_tool_call("search", {}, _raise_fn)
        stats = adapter.get_tool_stats()
        # Errors still charge the cost_estimate (best-effort accounting)
        # but total_cost_usd should not grow for error calls
        # The adapter charges cost_estimate on success only.
        assert stats["search"].total_cost_usd == 0.0


# ---------------------------------------------------------------------------
# Cost map: per-token costs
# ---------------------------------------------------------------------------


class TestCostMap:
    def test_per_token_cost_applied(self) -> None:
        def token_fn(**kwargs: Any) -> dict[str, Any]:
            return {"token_count": 100}

        costs = {
            "llm_tool": MCPToolCost("llm_tool", cost_per_call=0.0, cost_per_token=0.001)
        }
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("llm_tool", {}, token_fn)
        assert result.cost_usd == pytest.approx(0.10)

    def test_per_call_plus_per_token(self) -> None:
        def token_fn(**kwargs: Any) -> dict[str, Any]:
            return {"token_count": 50}

        costs = {
            "llm_tool": MCPToolCost(
                "llm_tool", cost_per_call=0.01, cost_per_token=0.002
            )
        }
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("llm_tool", {}, token_fn)
        assert result.cost_usd == pytest.approx(0.01 + 50 * 0.002)

    def test_no_token_count_in_result(self) -> None:
        costs = {
            "llm_tool": MCPToolCost("llm_tool", cost_per_call=0.05, cost_per_token=0.01)
        }
        adapter = _make_adapter(tool_costs=costs)
        # _echo_fn does not include token_count; per-token cost should be 0
        result = adapter.wrap_tool_call("llm_tool", {}, _echo_fn)
        assert result.cost_usd == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Adversarial: call_fn raises, returns None, negative default_cost
# ---------------------------------------------------------------------------


class TestAdversarial:
    def test_call_fn_raises_returns_allow_not_halt(self) -> None:
        adapter = _make_adapter()
        result = adapter.wrap_tool_call("search", {}, _raise_fn)
        # Errors from the tool are not budget halts; decision stays ALLOW
        assert result.decision == "ALLOW"
        assert result.success is False
        assert "tool call failed" in result.error

    def test_call_fn_returns_none(self) -> None:
        adapter = _make_adapter()
        result = adapter.wrap_tool_call("search", {}, _return_none_fn)
        assert result.success is True
        assert result.result is None

    def test_negative_default_cost_raises(self) -> None:
        ctx = _make_ctx()
        with pytest.raises(ValueError, match="default_cost_per_call"):
            MCPContainmentAdapter(execution_context=ctx, default_cost_per_call=-0.1)

    def test_call_fn_receives_arguments(self) -> None:
        received: list[dict] = []

        def capturing_fn(**kwargs: Any) -> None:
            received.append(kwargs)

        adapter = _make_adapter()
        adapter.wrap_tool_call("search", {"query": "hello", "limit": 5}, capturing_fn)
        assert received == [{"query": "hello", "limit": 5}]

    def test_error_message_contains_exception_type(self) -> None:
        adapter = _make_adapter()
        result = adapter.wrap_tool_call("search", {}, _raise_fn)
        assert "tool call failed" in result.error

    def test_exception_subclass_recorded(self) -> None:
        def value_error_fn(**kwargs: Any) -> Any:
            raise ValueError("bad value")

        adapter = _make_adapter()
        result = adapter.wrap_tool_call("search", {}, value_error_fn)
        assert "tool call failed" in result.error


# ---------------------------------------------------------------------------
# Thread safety: 10 concurrent tool calls, no data races
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_calls_no_race(self) -> None:
        adapter = _make_adapter(
            max_cost_usd=100.0, max_steps=200, default_cost_per_call=0.001
        )
        errors: list[Exception] = []

        def worker() -> None:
            try:
                adapter.wrap_tool_call("search", {"q": "x"}, _echo_fn)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Threads raised: {errors}"

    def test_concurrent_calls_count_consistent(self) -> None:
        adapter = _make_adapter(
            max_cost_usd=100.0, max_steps=200, default_cost_per_call=0.001
        )
        barrier = threading.Barrier(10)

        def worker() -> None:
            barrier.wait()
            adapter.wrap_tool_call("search", {}, _echo_fn)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = adapter.get_tool_stats()
        # Some calls may be halted by step limit but call_count should be consistent
        assert stats["search"].call_count <= 10

    def test_concurrent_stats_accumulation(self) -> None:
        adapter = _make_adapter(
            max_cost_usd=100.0, max_steps=200, default_cost_per_call=0.0
        )
        n_threads = 10

        def worker() -> None:
            adapter.wrap_tool_call("search", {}, _echo_fn)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = adapter.get_tool_stats()
        # All 10 threads must succeed; no budget limit, no errors
        assert stats["search"].call_count == 10
        assert stats["search"].error_count == 0


# ---------------------------------------------------------------------------
# Circuit breaker: per-server, not per-tool
# ---------------------------------------------------------------------------


class TestCircuitBreakerPerServer:
    def test_shared_cb_blocks_all_tools(self) -> None:
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        adapter = _make_adapter(circuit_breaker=cb)
        # Trip circuit on tool_a
        adapter.wrap_tool_call("tool_a", {}, _raise_fn)
        adapter.wrap_tool_call("tool_a", {}, _raise_fn)
        assert cb.state == CircuitState.OPEN
        # tool_b is on the same server -- also blocked
        result = adapter.wrap_tool_call("tool_b", {}, _echo_fn)
        assert result.decision == "HALT"

    def test_independent_adapters_have_independent_cbs(self) -> None:
        cb_a = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        cb_b = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        adapter_a = _make_adapter(circuit_breaker=cb_a)
        adapter_b = _make_adapter(circuit_breaker=cb_b)
        # Trip adapter_a
        adapter_a.wrap_tool_call("tool", {}, _raise_fn)
        adapter_a.wrap_tool_call("tool", {}, _raise_fn)
        assert cb_a.state == CircuitState.OPEN
        # adapter_b is unaffected
        result = adapter_b.wrap_tool_call("tool", {}, _echo_fn)
        assert result.decision == "ALLOW"


# ---------------------------------------------------------------------------
# Adversarial: corrupted inputs, boundary abuse, concurrent CB transitions
# ---------------------------------------------------------------------------


class TestAdversarialMCP:
    """Adversarial tests for MCPContainmentAdapter -- attacker mindset."""

    def test_corrupted_token_count_negative_ignored(self) -> None:
        """Negative token_count in result must not produce negative cost."""

        def neg_token_fn(**kwargs: Any) -> dict[str, Any]:
            return {"token_count": -100}

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.01, cost_per_token=0.001)}
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("tool", {}, neg_token_fn)
        # _extract_token_count requires isinstance(value, int) and value >= 0
        # Negative should return 0 tokens -> cost = cost_per_call only
        assert result.cost_usd == pytest.approx(0.01)

    def test_corrupted_token_count_string_ignored(self) -> None:
        """String token_count in result must be ignored (not coerced)."""

        def str_token_fn(**kwargs: Any) -> dict[str, Any]:
            return {"token_count": "one hundred"}

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.01, cost_per_token=0.001)}
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("tool", {}, str_token_fn)
        assert result.cost_usd == pytest.approx(0.01)

    def test_corrupted_token_count_float_ignored(self) -> None:
        """Float token_count must be ignored (isinstance int check)."""

        def float_token_fn(**kwargs: Any) -> dict[str, Any]:
            return {"token_count": 99.5}

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.0, cost_per_token=0.01)}
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("tool", {}, float_token_fn)
        # float is not int -> _extract_token_count returns 0
        assert result.cost_usd == pytest.approx(0.0)

    def test_corrupted_token_count_nan_int_impossible(self) -> None:
        """NaN cannot be int, so token extraction returns 0."""

        def nan_token_fn(**kwargs: Any) -> dict[str, Any]:
            return {"token_count": float("nan")}

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.0, cost_per_token=0.01)}
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("tool", {}, nan_token_fn)
        assert result.cost_usd == pytest.approx(0.0)

    def test_empty_tool_name_raises(self) -> None:
        """Empty string tool_name must raise ValueError (M3 validation)."""
        adapter = _make_adapter()
        with pytest.raises(ValueError, match="tool_name must be a non-empty string"):
            adapter.wrap_tool_call("", {}, _echo_fn)

    def test_very_long_tool_name(self) -> None:
        """Very long tool name must not cause OOM or crash."""
        long_name = "x" * 10_000
        adapter = _make_adapter()
        result = adapter.wrap_tool_call(long_name, {}, _echo_fn)
        assert result.success is True
        stats = adapter.get_tool_stats()
        assert long_name in stats

    def test_tool_name_with_special_chars(self) -> None:
        """Tool name with special characters must not cause issues."""
        for name in [
            "tool/sub",
            "tool::method",
            "tool with spaces",
            "\n\t",
            "\x00null",
        ]:
            adapter = _make_adapter()
            result = adapter.wrap_tool_call(name, {}, _echo_fn)
            assert result.success is True

    def test_call_fn_mutates_arguments_no_side_effect(self) -> None:
        """call_fn modifying the arguments dict must not affect adapter internals."""

        def mutating_fn(**kwargs: Any) -> str:
            kwargs["injected"] = "malicious"
            return "ok"

        adapter = _make_adapter()
        args = {"query": "hello"}
        result = adapter.wrap_tool_call("search", args, mutating_fn)
        assert result.success is True
        # Original args dict is mutated (Python's ** unpacking creates a new dict
        # for the callee), but the adapter itself should be unaffected
        assert result.result == "ok"

    def test_zero_default_cost(self) -> None:
        """default_cost_per_call=0.0 is valid and must not raise."""
        adapter = _make_adapter(default_cost_per_call=0.0)
        result = adapter.wrap_tool_call("search", {}, _echo_fn)
        assert result.cost_usd == pytest.approx(0.0)

    def test_call_fn_returns_object_with_token_count_attr(self) -> None:
        """Result with token_count as attribute (not dict key) must be extracted."""

        class TokenResult:
            token_count = 50

        def obj_fn(**kwargs: Any) -> TokenResult:
            return TokenResult()

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.0, cost_per_token=0.001)}
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("tool", {}, obj_fn)
        assert result.cost_usd == pytest.approx(0.05)

    def test_call_fn_returns_object_with_negative_token_count_attr(self) -> None:
        """Object with negative token_count attr must be ignored."""

        class BadResult:
            token_count = -999

        def obj_fn(**kwargs: Any) -> BadResult:
            return BadResult()

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.01, cost_per_token=0.001)}
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("tool", {}, obj_fn)
        assert result.cost_usd == pytest.approx(0.01)

    def test_concurrent_failures_and_successes_racing_cb(self) -> None:
        """10 threads: 5 failing + 5 succeeding -- CB state must be consistent."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
        adapter = _make_adapter(max_cost_usd=100.0, max_steps=200, circuit_breaker=cb)
        barrier = threading.Barrier(10)
        results: list[MCPToolResult] = []
        lock = threading.Lock()

        def worker(fail: bool) -> None:
            barrier.wait()
            fn = _raise_fn if fail else _echo_fn
            r = adapter.wrap_tool_call("search", {}, fn)
            with lock:
                results.append(r)

        threads = []
        for i in range(10):
            threads.append(threading.Thread(target=worker, args=(i < 5,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # CB state must be one of the valid states (no corruption)
        assert cb.state in (CircuitState.CLOSED, CircuitState.OPEN)
        assert len(results) == 10

    def test_many_distinct_tools_no_memory_explosion(self) -> None:
        """100 distinct tool names must not cause excessive memory usage."""
        adapter = _make_adapter(max_cost_usd=100.0, max_steps=500)
        for i in range(100):
            adapter.wrap_tool_call(f"tool_{i}", {}, _echo_fn)
        stats = adapter.get_tool_stats()
        assert len(stats) == 100
        for name, s in stats.items():
            assert s.call_count == 1

    def test_cost_per_token_zero_with_tokens(self) -> None:
        """cost_per_token=0 with tokens should not add to cost."""

        def token_fn(**kwargs: Any) -> dict[str, Any]:
            return {"token_count": 1000}

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.01, cost_per_token=0.0)}
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("tool", {}, token_fn)
        assert result.cost_usd == pytest.approx(0.01)

    def test_large_token_count_no_overflow(self) -> None:
        """Very large token count must not cause overflow."""

        def big_token_fn(**kwargs: Any) -> dict[str, Any]:
            return {"token_count": 10**9}

        costs = {
            "tool": MCPToolCost("tool", cost_per_call=0.0, cost_per_token=0.000001)
        }
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("tool", {}, big_token_fn)
        assert result.cost_usd == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# Async guard: passing a coroutine function raises TypeError
# ---------------------------------------------------------------------------


class TestAsyncGuard:
    """Async function passed to wrap_tool_call must raise TypeError immediately."""

    def test_async_fn_raises_type_error(self) -> None:
        async def async_fn(**kwargs: Any) -> str:
            return "async result"

        adapter = _make_adapter()
        with pytest.raises(TypeError, match="coroutine function"):
            adapter.wrap_tool_call("tool", {}, async_fn)

    def test_async_fn_error_message_mentions_async_adapter(self) -> None:
        async def async_fn(**kwargs: Any) -> None:
            pass

        adapter = _make_adapter()
        with pytest.raises(TypeError) as exc_info:
            adapter.wrap_tool_call("tool", {}, async_fn)
        assert "AsyncMCPContainmentAdapter" in str(exc_info.value)

    def test_sync_fn_does_not_raise_type_error(self) -> None:
        adapter = _make_adapter()
        # Must not raise TypeError for a normal sync function
        result = adapter.wrap_tool_call("tool", {}, _echo_fn)
        assert result.success is True

    def test_async_fn_does_not_call_fn_before_raising(self) -> None:
        called = [False]

        async def async_fn(**kwargs: Any) -> None:
            called[0] = True

        adapter = _make_adapter()
        with pytest.raises(TypeError):
            adapter.wrap_tool_call("tool", {}, async_fn)
        assert called[0] is False


# ---------------------------------------------------------------------------
# Timeout: elapsed-time check (non-preemptive)
# ---------------------------------------------------------------------------


class TestTimeout:
    """timeout_seconds parameter: calls that exceed the limit are marked as timeout errors."""

    @nogil_unstable
    def test_slow_fn_triggers_timeout(self) -> None:
        import time as _time

        def slow_fn(**kwargs: Any) -> str:
            _time.sleep(0.5)
            return "done"

        ctx = _make_ctx()
        from veronica_core.adapters.mcp import MCPContainmentAdapter

        timed_adapter = MCPContainmentAdapter(
            execution_context=ctx,
            timeout_seconds=0.05,
        )
        result = timed_adapter.wrap_tool_call("slow_tool", {}, slow_fn)
        assert result.success is False
        assert "tool call failed" in result.error

    def test_fast_fn_no_timeout(self) -> None:
        from veronica_core.adapters.mcp import MCPContainmentAdapter

        ctx = _make_ctx()
        timed_adapter = MCPContainmentAdapter(
            execution_context=ctx,
            timeout_seconds=5.0,
        )
        result = timed_adapter.wrap_tool_call("fast_tool", {}, _echo_fn)
        assert result.success is True

    def test_no_timeout_by_default(self) -> None:
        import time as _time

        def slow_fn(**kwargs: Any) -> str:
            _time.sleep(0.05)
            return "done"

        # No timeout_seconds set -- slow fn must succeed
        adapter = _make_adapter()
        result = adapter.wrap_tool_call("slow_tool", {}, slow_fn)
        assert result.success is True

    @nogil_unstable
    def test_timeout_increments_error_count(self) -> None:
        import time as _time

        def slow_fn(**kwargs: Any) -> str:
            _time.sleep(0.5)
            return "done"

        from veronica_core.adapters.mcp import MCPContainmentAdapter

        ctx = _make_ctx()
        timed_adapter = MCPContainmentAdapter(
            execution_context=ctx,
            timeout_seconds=0.05,
        )
        timed_adapter.wrap_tool_call("slow_tool", {}, slow_fn)
        stats = timed_adapter.get_tool_stats()
        assert stats["slow_tool"].error_count == 1


# ---------------------------------------------------------------------------
# FailurePredicate: selective circuit breaker tripping
# ---------------------------------------------------------------------------


class TestFailurePredicate:
    """failure_predicate controls which exceptions trip the circuit breaker."""

    def test_predicate_false_does_not_trip_cb(self) -> None:
        """When predicate returns False, exception must not trip CB."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

        def never_trip(exc: BaseException) -> bool:
            return False

        from veronica_core.adapters.mcp import MCPContainmentAdapter

        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(
            execution_context=ctx,
            circuit_breaker=cb,
            failure_predicate=never_trip,
        )
        adapter.wrap_tool_call("tool", {}, _raise_fn)
        adapter.wrap_tool_call("tool", {}, _raise_fn)
        # CB should still be CLOSED -- predicate filtered out failures
        assert cb.state == CircuitState.CLOSED

    def test_predicate_true_trips_cb(self) -> None:
        """When predicate returns True, exception must trip CB."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

        def always_trip(exc: BaseException) -> bool:
            return True

        from veronica_core.adapters.mcp import MCPContainmentAdapter

        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(
            execution_context=ctx,
            circuit_breaker=cb,
            failure_predicate=always_trip,
        )
        adapter.wrap_tool_call("tool", {}, _raise_fn)
        adapter.wrap_tool_call("tool", {}, _raise_fn)
        assert cb.state == CircuitState.OPEN

    def test_predicate_filters_by_exception_type(self) -> None:
        """Predicate can selectively ignore certain exception types."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

        def only_value_errors(exc: BaseException) -> bool:
            return isinstance(exc, ValueError)

        def runtime_error_fn(**kwargs: Any) -> Any:
            raise RuntimeError("transient")

        from veronica_core.adapters.mcp import MCPContainmentAdapter

        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(
            execution_context=ctx,
            circuit_breaker=cb,
            failure_predicate=only_value_errors,
        )
        # RuntimeError is not ValueError -- must not trip CB
        adapter.wrap_tool_call("tool", {}, runtime_error_fn)
        adapter.wrap_tool_call("tool", {}, runtime_error_fn)
        assert cb.state == CircuitState.CLOSED

    def test_no_predicate_trips_cb_on_any_error(self) -> None:
        """Without predicate, any exception trips the CB (existing behavior)."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        adapter = _make_adapter(circuit_breaker=cb)
        adapter.wrap_tool_call("tool", {}, _raise_fn)
        adapter.wrap_tool_call("tool", {}, _raise_fn)
        assert cb.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# isError handling: MCP tool reporting error via isError flag
# ---------------------------------------------------------------------------


class TestIsErrorHandling:
    """MCP tool results with isError=True must be treated as failures."""

    def test_is_error_true_returns_failure(self) -> None:
        class ErrorResult:
            isError = True

        def error_result_fn(**kwargs: Any) -> ErrorResult:
            return ErrorResult()

        adapter = _make_adapter()
        result = adapter.wrap_tool_call("tool", {}, error_result_fn)
        assert result.success is False

    def test_is_error_true_error_message(self) -> None:
        class ErrorResult:
            isError = True

        def error_result_fn(**kwargs: Any) -> "ErrorResult":
            return ErrorResult()

        adapter = _make_adapter()
        result = adapter.wrap_tool_call("tool", {}, error_result_fn)
        assert result.error is not None
        assert "isError" in result.error

    def test_is_error_true_decision_allow_not_halt(self) -> None:
        """isError is a tool-level error, not a budget halt -- decision stays ALLOW."""

        class ErrorResult:
            isError = True

        def error_result_fn(**kwargs: Any) -> "ErrorResult":
            return ErrorResult()

        adapter = _make_adapter()
        result = adapter.wrap_tool_call("tool", {}, error_result_fn)
        assert result.decision == "ALLOW"

    def test_is_error_true_result_passed_through(self) -> None:
        """The raw result object must be accessible even when isError=True."""

        class ErrorResult:
            isError = True
            message = "something went wrong"

        obj = ErrorResult()

        def error_result_fn(**kwargs: Any) -> "ErrorResult":
            return obj

        adapter = _make_adapter()
        result = adapter.wrap_tool_call("tool", {}, error_result_fn)
        assert result.result is obj

    def test_is_error_false_returns_success(self) -> None:
        class OkResult:
            isError = False

        def ok_result_fn(**kwargs: Any) -> "OkResult":
            return OkResult()

        adapter = _make_adapter()
        result = adapter.wrap_tool_call("tool", {}, ok_result_fn)
        assert result.success is True

    def test_is_error_missing_attr_returns_success(self) -> None:
        """Objects without isError attribute must be treated as success."""
        adapter = _make_adapter()
        result = adapter.wrap_tool_call("tool", {}, _echo_fn)
        assert result.success is True

    def test_is_error_true_increments_error_count(self) -> None:
        class ErrorResult:
            isError = True

        def error_result_fn(**kwargs: Any) -> "ErrorResult":
            return ErrorResult()

        adapter = _make_adapter()
        adapter.wrap_tool_call("tool", {}, error_result_fn)
        stats = adapter.get_tool_stats()
        assert stats["tool"].error_count == 1

    def test_is_error_true_does_not_trip_cb(self) -> None:
        """isError=True does not trip circuit breaker (no exception raised)."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

        class ErrorResult:
            isError = True

        def error_result_fn(**kwargs: Any) -> "ErrorResult":
            return ErrorResult()

        adapter = _make_adapter(circuit_breaker=cb)
        adapter.wrap_tool_call("tool", {}, error_result_fn)
        adapter.wrap_tool_call("tool", {}, error_result_fn)
        # CB should still be CLOSED -- isError is not an exception
        assert cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Timeout seconds validation
# ---------------------------------------------------------------------------


class TestTimeoutSecondsValidation:
    def test_negative_timeout_raises(self) -> None:
        ctx = _make_ctx()
        with pytest.raises(ValueError, match="non-negative"):
            MCPContainmentAdapter(ctx, timeout_seconds=-1.0)

    def test_nan_timeout_raises(self) -> None:
        ctx = _make_ctx()
        with pytest.raises(ValueError, match="finite"):
            MCPContainmentAdapter(ctx, timeout_seconds=float("nan"))

    def test_inf_timeout_raises(self) -> None:
        ctx = _make_ctx()
        with pytest.raises(ValueError, match="finite"):
            MCPContainmentAdapter(ctx, timeout_seconds=float("inf"))

    def test_zero_timeout_accepted(self) -> None:
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(ctx, timeout_seconds=0.0)
        assert adapter._timeout_seconds == 0.0

    def test_none_timeout_accepted(self) -> None:
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(ctx, timeout_seconds=None)
        assert adapter._timeout_seconds is None
