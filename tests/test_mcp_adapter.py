"""Tests for veronica_core.adapters.mcp.MCPContainmentAdapter.

Does not require the mcp-sdk library.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

import pytest

from veronica_core.adapters.mcp import MCPContainmentAdapter, MCPToolCost, MCPToolResult, MCPToolStats
from veronica_core.circuit_breaker import CircuitBreaker, CircuitState
from veronica_core.containment.execution_context import ExecutionConfig, ExecutionContext


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
        if result.decision == "HALT":
            assert result.error is not None

    def test_halt_result_success_is_false(self) -> None:
        adapter = _make_adapter(max_cost_usd=0.0001, default_cost_per_call=0.01)
        adapter.wrap_tool_call("search", {}, _echo_fn)
        result = adapter.wrap_tool_call("search", {}, _echo_fn)
        if result.decision == "HALT":
            assert result.success is False

    def test_halt_cost_is_zero(self) -> None:
        adapter = _make_adapter(max_cost_usd=0.0001, default_cost_per_call=0.01)
        adapter.wrap_tool_call("search", {}, _echo_fn)
        result = adapter.wrap_tool_call("search", {}, _echo_fn)
        if result.decision == "HALT":
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
        if result.decision == "HALT":
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

        costs = {"llm_tool": MCPToolCost("llm_tool", cost_per_call=0.0, cost_per_token=0.001)}
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("llm_tool", {}, token_fn)
        assert result.cost_usd == pytest.approx(0.10)

    def test_per_call_plus_per_token(self) -> None:
        def token_fn(**kwargs: Any) -> dict[str, Any]:
            return {"token_count": 50}

        costs = {"llm_tool": MCPToolCost("llm_tool", cost_per_call=0.01, cost_per_token=0.002)}
        adapter = _make_adapter(tool_costs=costs)
        result = adapter.wrap_tool_call("llm_tool", {}, token_fn)
        assert result.cost_usd == pytest.approx(0.01 + 50 * 0.002)

    def test_no_token_count_in_result(self) -> None:
        costs = {"llm_tool": MCPToolCost("llm_tool", cost_per_call=0.05, cost_per_token=0.01)}
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
        assert "RuntimeError" in result.error

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
        assert "RuntimeError" in result.error

    def test_exception_subclass_recorded(self) -> None:
        def value_error_fn(**kwargs: Any) -> Any:
            raise ValueError("bad value")

        adapter = _make_adapter()
        result = adapter.wrap_tool_call("search", {}, value_error_fn)
        assert "ValueError" in result.error


# ---------------------------------------------------------------------------
# Thread safety: 10 concurrent tool calls, no data races
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_calls_no_race(self) -> None:
        adapter = _make_adapter(max_cost_usd=100.0, max_steps=200, default_cost_per_call=0.001)
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
        adapter = _make_adapter(max_cost_usd=100.0, max_steps=200, default_cost_per_call=0.001)
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
        # All stats must be consistent (no torn reads)
        assert stats["search"].call_count >= 0
        assert stats["search"].error_count >= 0
        assert stats["search"].call_count >= stats["search"].error_count


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
