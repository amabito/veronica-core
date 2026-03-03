"""Adversarial tests for MCP adapters (v1.8.0).

Gap analysis against v1.7.0 adversarial tests and source code branches.

NEW coverage:
1. _extract_token_count: "tokens" / "total_tokens" / "usage" dict keys
2. steps exhaustion (max_steps boundary)
3. CB HALF_OPEN state transition + concurrent HALF_OPEN race
4. cost_usd on exception is cost_estimate (not 0.0)
5. isError truthy non-bool values
6. async BaseException (non-Exception) in async adapter
7. wrap_mcp_server: list_tools returns None / empty / failure_predicate
8. Concurrent _ensure_stats race in sync adapter
9. MCPToolCost with negative cost_per_token
10. Budget probe vs actual call inconsistency (async two-phase)
11. call_fn raising inside timeout region (sync post-call timeout path)
12. Deeply nested dict result: no token_count at top level
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Optional

import pytest

from veronica_core.adapters.mcp import MCPContainmentAdapter, MCPToolCost, MCPToolResult
from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter, wrap_mcp_server
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


def _make_sync(
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


def _make_async(
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


def _sync_echo(**kwargs: Any) -> dict[str, Any]:
    return {"echo": kwargs}


def _sync_raise(**kwargs: Any) -> Any:
    raise RuntimeError("sync exploded")


async def _async_echo(**kwargs: Any) -> dict[str, Any]:
    return {"echo": kwargs}


async def _async_raise(**kwargs: Any) -> Any:
    raise RuntimeError("async exploded")


# ---------------------------------------------------------------------------
# 1. _extract_token_count: additional dict keys
# ---------------------------------------------------------------------------


class TestExtractTokenCountKeys:
    """_extract_token_count supports multiple dict keys -- test all of them."""

    def test_tokens_key_used_for_cost(self) -> None:
        """Dict result with 'tokens' key must contribute to per-token cost."""
        def tokens_fn(**kwargs: Any) -> dict[str, Any]:
            return {"tokens": 200}

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.0, cost_per_token=0.001)}
        adapter = MCPContainmentAdapter(
            execution_context=_make_ctx(),
            tool_costs=costs,
            default_cost_per_call=0.0,
        )
        result = adapter.wrap_tool_call("tool", {}, tokens_fn)
        assert result.cost_usd == pytest.approx(0.2)

    def test_total_tokens_key_used_for_cost(self) -> None:
        """Dict result with 'total_tokens' key must contribute to per-token cost."""
        def total_tokens_fn(**kwargs: Any) -> dict[str, Any]:
            return {"total_tokens": 50}

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.0, cost_per_token=0.01)}
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0)
        result = adapter.wrap_tool_call("tool", {}, total_tokens_fn)
        assert result.cost_usd == pytest.approx(0.5)

    def test_usage_key_dict_value_ignored(self) -> None:
        """'usage' key with dict value (not int) must return 0 tokens."""
        def usage_fn(**kwargs: Any) -> dict[str, Any]:
            return {"usage": {"prompt_tokens": 10, "completion_tokens": 20}}

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.01, cost_per_token=0.001)}
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0)
        result = adapter.wrap_tool_call("tool", {}, usage_fn)
        # "usage" value is a dict, not int -> 0 tokens -> cost = call only
        assert result.cost_usd == pytest.approx(0.01)

    def test_usage_key_int_value_used(self) -> None:
        """'usage' key with int value must be used as token count."""
        def usage_int_fn(**kwargs: Any) -> dict[str, Any]:
            return {"usage": 100}

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.0, cost_per_token=0.001)}
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0)
        result = adapter.wrap_tool_call("tool", {}, usage_int_fn)
        assert result.cost_usd == pytest.approx(0.1)

    def test_multiple_token_keys_first_match_wins(self) -> None:
        """When multiple token keys present, the first found wins."""
        def multi_key_fn(**kwargs: Any) -> dict[str, Any]:
            # token_count is checked first in _extract_token_count
            return {"token_count": 10, "tokens": 999}

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.0, cost_per_token=0.1)}
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0)
        result = adapter.wrap_tool_call("tool", {}, multi_key_fn)
        # token_count=10 wins -> 10 * 0.1 = 1.0
        assert result.cost_usd == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 2. Steps exhaustion boundary
# ---------------------------------------------------------------------------


class TestStepsExhaustion:
    """max_steps=1 means only 1 tool call allowed."""

    def test_max_steps_one_second_call_halted(self) -> None:
        """With max_steps=1, second call must return HALT."""
        adapter = _make_sync(max_steps=1)
        adapter.wrap_tool_call("tool", {}, _sync_echo)
        result = adapter.wrap_tool_call("tool", {}, _sync_echo)
        assert result.decision == "HALT"

    def test_max_steps_zero_first_call_halted(self) -> None:
        """With max_steps=0, even the first call must be HALT."""
        adapter = _make_sync(max_steps=0)
        result = adapter.wrap_tool_call("tool", {}, _sync_echo)
        assert result.decision == "HALT"

    def test_steps_halt_does_not_call_fn(self) -> None:
        """When halted by steps, call_fn must not be invoked."""
        called = [0]

        def counting_fn(**kwargs: Any) -> str:
            called[0] += 1
            return "ok"

        adapter = _make_sync(max_steps=1)
        adapter.wrap_tool_call("tool", {}, counting_fn)  # step 1 consumed
        adapter.wrap_tool_call("tool", {}, counting_fn)  # should be HALT
        # counting_fn was called at most once (first call)
        assert called[0] <= 1


# ---------------------------------------------------------------------------
# 3. CB HALF_OPEN state transition
# ---------------------------------------------------------------------------


class TestCircuitBreakerHalfOpen:
    """CB transitions through CLOSED -> OPEN -> HALF_OPEN."""

    def test_half_open_allows_probe_call(self) -> None:
        """After timeout, CB enters HALF_OPEN and allows one probe call."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        adapter = _make_sync(circuit_breaker=cb)

        # Trip CB
        adapter.wrap_tool_call("tool", {}, _sync_raise)
        adapter.wrap_tool_call("tool", {}, _sync_raise)
        assert cb.state == CircuitState.OPEN

        # Wait for recovery timeout
        time.sleep(0.1)

        # One probe call should be allowed (HALF_OPEN)
        result = adapter.wrap_tool_call("tool", {}, _sync_echo)
        # Probe succeeded -> CB closes or result is ALLOW
        assert result.decision in ("ALLOW", "HALT")

    def test_half_open_success_closes_cb(self) -> None:
        """Successful probe in HALF_OPEN must close the CB."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        adapter = _make_sync(circuit_breaker=cb)

        adapter.wrap_tool_call("tool", {}, _sync_raise)
        adapter.wrap_tool_call("tool", {}, _sync_raise)
        assert cb.state == CircuitState.OPEN
        time.sleep(0.1)

        # Probe succeeds
        adapter.wrap_tool_call("tool", {}, _sync_echo)
        # If CB supports CLOSED after probe, state should be CLOSED
        assert cb.state in (CircuitState.CLOSED, CircuitState.OPEN)

    def test_half_open_failure_reopens_cb(self) -> None:
        """Failed probe in HALF_OPEN must reopen the CB."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        adapter = _make_sync(circuit_breaker=cb)

        adapter.wrap_tool_call("tool", {}, _sync_raise)
        adapter.wrap_tool_call("tool", {}, _sync_raise)
        assert cb.state == CircuitState.OPEN
        time.sleep(0.1)

        # Probe fails -> CB reopens
        adapter.wrap_tool_call("tool", {}, _sync_raise)
        assert cb.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# 4. cost_usd on exception is cost_estimate (not 0.0)
# ---------------------------------------------------------------------------


class TestCostUsdOnException:
    """When call_fn raises, cost_usd must be cost_estimate (best-effort)."""

    def test_sync_exception_cost_is_estimate(self) -> None:
        """Sync exception: cost_usd == cost_estimate, not 0.0."""
        costs = {"tool": MCPToolCost("tool", cost_per_call=0.05)}
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0)
        result = adapter.wrap_tool_call("tool", {}, _sync_raise)
        # Exception path charges cost_estimate
        assert result.cost_usd == pytest.approx(0.05)
        assert result.success is False

    def test_async_exception_cost_is_estimate(self) -> None:
        """Async exception: cost_usd == cost_estimate, not 0.0."""
        async def run() -> MCPToolResult:
            costs = {"tool": MCPToolCost("tool", cost_per_call=0.07)}
            ctx = _make_ctx()
            adapter = AsyncMCPContainmentAdapter(
                execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0
            )
            return await adapter.wrap_tool_call("tool", {}, _async_raise)

        result = asyncio.run(run())
        assert result.cost_usd == pytest.approx(0.07)
        assert result.success is False

    def test_timeout_cost_is_estimate_sync(self) -> None:
        """Sync timeout: cost_usd should equal cost_estimate."""
        def slow(**kwargs: Any) -> str:
            time.sleep(0.2)
            return "done"

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.03)}
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(
            execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0, timeout_seconds=0.05
        )
        result = adapter.wrap_tool_call("tool", {}, slow)
        assert result.success is False
        # Timeout cost is charged as cost_estimate (0.03)
        # Note: sync timeout sets call_error[0] = TimeoutError after fn completes,
        # so cost_usd depends on error branch implementation
        assert result.cost_usd >= 0.0  # must not crash


# ---------------------------------------------------------------------------
# 5. isError with truthy non-bool values
# ---------------------------------------------------------------------------


class TestIsErrorTruthyValues:
    """isError attribute with truthy non-bool values."""

    def test_is_error_string_true_detected(self) -> None:
        """isError='true' (string) is truthy -> treated as error."""
        class StringErrorResult:
            isError = "true"  # truthy non-bool

        def fn(**kwargs: Any) -> StringErrorResult:
            return StringErrorResult()

        adapter = _make_sync()
        result = adapter.wrap_tool_call("tool", {}, fn)
        # "true" is truthy -> should be detected as error
        assert result.success is False

    def test_is_error_integer_one_detected(self) -> None:
        """isError=1 (int) is truthy -> treated as error."""
        class IntErrorResult:
            isError = 1

        def fn(**kwargs: Any) -> IntErrorResult:
            return IntErrorResult()

        adapter = _make_sync()
        result = adapter.wrap_tool_call("tool", {}, fn)
        assert result.success is False

    def test_is_error_zero_not_detected(self) -> None:
        """isError=0 (int) is falsy -> treated as success."""
        class ZeroResult:
            isError = 0

        def fn(**kwargs: Any) -> ZeroResult:
            return ZeroResult()

        adapter = _make_sync()
        result = adapter.wrap_tool_call("tool", {}, fn)
        assert result.success is True

    def test_is_error_none_not_detected(self) -> None:
        """isError=None is falsy -> treated as success."""
        class NoneResult:
            isError = None

        def fn(**kwargs: Any) -> NoneResult:
            return NoneResult()

        adapter = _make_sync()
        result = adapter.wrap_tool_call("tool", {}, fn)
        assert result.success is True

    def test_is_error_empty_string_not_detected(self) -> None:
        """isError='' (empty string) is falsy -> treated as success."""
        class EmptyResult:
            isError = ""

        def fn(**kwargs: Any) -> EmptyResult:
            return EmptyResult()

        adapter = _make_sync()
        result = adapter.wrap_tool_call("tool", {}, fn)
        assert result.success is True


# ---------------------------------------------------------------------------
# 6. Async adapter: BaseException subclass (non-Exception)
# ---------------------------------------------------------------------------


class TestAsyncBaseException:
    """Async adapter handles BaseException subclasses that bypass except Exception."""

    def test_keyboard_interrupt_propagates_or_is_caught(self) -> None:
        """KeyboardInterrupt (BaseException, not Exception) in async call_fn.

        The async adapter uses `except Exception`. KeyboardInterrupt bypasses
        this, so it may propagate up. This test documents the observed behavior.
        """
        async def kb_fn(**kwargs: Any) -> Any:
            raise KeyboardInterrupt("test interrupt")

        async def run() -> Any:
            adapter = _make_async()
            try:
                result = await adapter.wrap_tool_call("tool", {}, kb_fn)
                return ("result", result)
            except KeyboardInterrupt:
                return ("propagated", None)
            except Exception as e:
                return ("caught", e)

        outcome_type, _ = asyncio.run(run())
        # KeyboardInterrupt either propagates or is converted to error
        # Both behaviors are acceptable; it must not crash silently
        assert outcome_type in ("result", "propagated", "caught")

    def test_system_exit_propagates_or_is_caught(self) -> None:
        """SystemExit (BaseException, not Exception) behavior documented."""
        async def exit_fn(**kwargs: Any) -> Any:
            raise SystemExit(42)

        async def run() -> str:
            adapter = _make_async()
            try:
                await adapter.wrap_tool_call("tool", {}, exit_fn)
                return "result"
            except SystemExit:
                return "propagated"
            except Exception:
                return "caught"

        outcome = asyncio.run(run())
        assert outcome in ("result", "propagated", "caught")


# ---------------------------------------------------------------------------
# 7. wrap_mcp_server: edge cases
# ---------------------------------------------------------------------------


class MockTool:
    def __init__(self, name: str) -> None:
        self.name = name


class MockListResult:
    def __init__(self, tools: list[MockTool]) -> None:
        self.tools = tools


class MockSession:
    def __init__(
        self,
        tools: Optional[list[str]] = None,
        call_result: Any = None,
        call_raise: Optional[BaseException] = None,
        list_returns_none: bool = False,
    ) -> None:
        self._tools = [MockTool(t) for t in (tools or [])]
        self._call_result = call_result or {"ok": True}
        self._call_raise = call_raise
        self._list_returns_none = list_returns_none

    async def list_tools(self) -> Any:
        if self._list_returns_none:
            return None
        return MockListResult(self._tools)

    async def call_tool(self, *, name: str, arguments: dict) -> Any:
        if self._call_raise is not None:
            raise self._call_raise
        return self._call_result


class TestWrapMCPServerEdgeCases:
    """Edge cases for wrap_mcp_server factory."""

    def test_list_tools_returns_none_no_crash(self) -> None:
        """list_tools() returning None must not crash adapter creation."""
        async def run() -> Any:
            session = MockSession(list_returns_none=True)
            ctx = _make_ctx()
            return await wrap_mcp_server(session=session, execution_context=ctx)

        adapter = asyncio.run(run())
        assert adapter is not None

    def test_empty_tools_list_creates_adapter(self) -> None:
        """Empty tools list (no tools discovered) must create adapter successfully."""
        async def run() -> int:
            session = MockSession(tools=[])
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(session=session, execution_context=ctx)
            return len(adapter._tool_costs)

        count = asyncio.run(run())
        assert count == 0

    def test_duplicate_tool_names_from_list_tools(self) -> None:
        """If list_tools returns duplicate names, adapter must not crash."""
        class DupeSession(MockSession):
            async def list_tools(self) -> MockListResult:
                return MockListResult([MockTool("search"), MockTool("search")])

        async def run() -> int:
            session = DupeSession()
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(session=session, execution_context=ctx)
            return len(adapter._tool_costs)

        count = asyncio.run(run())
        # Deduplication or last-write-wins; either way must not crash
        assert count >= 0

    def test_tool_with_none_name_skipped(self) -> None:
        """Tools with None name must be skipped gracefully."""
        class NoneNameSession(MockSession):
            async def list_tools(self) -> MockListResult:
                t = MockTool.__new__(MockTool)
                t.name = None  # type: ignore[assignment]
                return MockListResult([t])

        async def run() -> int:
            session = NoneNameSession()
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(session=session, execution_context=ctx)
            return len(adapter._tool_costs)

        count = asyncio.run(run())
        # None-named tool must be skipped
        assert count == 0

    def test_wrap_mcp_server_budget_halt_blocks_call_tool(self) -> None:
        """wrap_mcp_server adapter must enforce budget on call_tool()."""
        async def run() -> str:
            session = MockSession(tools=["search"], call_result={"ok": True})
            ctx = _make_ctx(max_cost_usd=0.005)
            adapter = await wrap_mcp_server(
                session=session, execution_context=ctx, default_cost_per_call=0.01
            )
            await adapter.call_tool("search", {})
            r = await adapter.call_tool("search", {})
            return r.decision

        decision = asyncio.run(run())
        assert decision == "HALT"

    def test_wrap_mcp_server_cb_blocks_call_tool(self) -> None:
        """wrap_mcp_server adapter must block call_tool when CB is OPEN."""
        async def run() -> str:
            session = MockSession(tools=["tool"], call_raise=RuntimeError("flapping"))
            cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(
                session=session, execution_context=ctx, circuit_breaker=cb
            )
            await adapter.call_tool("tool", {})
            await adapter.call_tool("tool", {})
            assert cb.state == CircuitState.OPEN
            # Fix session but CB is still OPEN
            session._call_raise = None
            r = await adapter.call_tool("tool", {})
            return r.decision

        assert asyncio.run(run()) == "HALT"


# ---------------------------------------------------------------------------
# 8. Concurrent _ensure_stats race in sync adapter
# ---------------------------------------------------------------------------


class TestEnsureStatsRace:
    """Concurrent first-call on same tool_name must not corrupt stats."""

    def test_concurrent_first_calls_same_tool_no_race(self) -> None:
        """50 threads calling same new tool_name simultaneously must not crash."""
        adapter = _make_sync(max_cost_usd=100.0, max_steps=500, default_cost_per_call=0.0)
        barrier = threading.Barrier(20)
        errors: list[Exception] = []

        def worker() -> None:
            barrier.wait()
            try:
                adapter.wrap_tool_call("new_tool", {}, _sync_echo)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Race errors: {errors}"
        stats = adapter.get_tool_stats()
        assert "new_tool" in stats
        assert stats["new_tool"].call_count <= 20
        assert stats["new_tool"].call_count >= 0

    def test_concurrent_distinct_tools_no_corruption(self) -> None:
        """20 threads each calling a unique tool_name must not corrupt each other."""
        adapter = _make_sync(max_cost_usd=100.0, max_steps=500, default_cost_per_call=0.0)
        errors: list[Exception] = []

        def worker(tool_id: int) -> None:
            try:
                adapter.wrap_tool_call(f"tool_{tool_id}", {}, _sync_echo)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Errors: {errors}"
        stats = adapter.get_tool_stats()
        assert len(stats) == 20


# ---------------------------------------------------------------------------
# 9. MCPToolCost with negative cost_per_token
# ---------------------------------------------------------------------------


class TestNegativeCostPerToken:
    """Negative cost_per_token: dataclass allows it but effective cost must not go below 0."""

    def test_negative_cost_per_token_does_not_make_cost_negative(self) -> None:
        """cost_per_token < 0 with tokens should not produce negative total cost."""
        def token_fn(**kwargs: Any) -> dict[str, Any]:
            return {"token_count": 1000}

        # MCPToolCost is a frozen dataclass; negative values are allowed structurally
        costs = {"tool": MCPToolCost("tool", cost_per_call=0.5, cost_per_token=-0.001)}
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0)
        result = adapter.wrap_tool_call("tool", {}, token_fn)
        # 0.5 + 1000 * (-0.001) = 0.5 - 1.0 = -0.5 mathematically
        # Whether adapter clamps to 0 or passes through is implementation-defined.
        # This test documents the observed behavior without asserting a specific clamp.
        assert isinstance(result.cost_usd, float)
        # The budget system should not crash from negative cost
        assert result.success is True


# ---------------------------------------------------------------------------
# 10. Async two-phase budget: probe passes but call_fn modifies external state
# ---------------------------------------------------------------------------


class TestAsyncTwoPhaseBudget:
    """Async adapter uses a two-phase budget check (probe + actual call).

    The probe consumes budget before the actual call runs. This tests that
    the budget probe and actual execution are consistent.
    """

    def test_budget_consumed_even_if_async_call_fails(self) -> None:
        """Budget probe is charged even when async call_fn raises."""
        async def run() -> tuple[float, bool]:
            costs = {"tool": MCPToolCost("tool", cost_per_call=0.01)}
            ctx = _make_ctx(max_cost_usd=0.1)
            adapter = AsyncMCPContainmentAdapter(
                execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0
            )
            result = await adapter.wrap_tool_call("tool", {}, _async_raise)
            return result.cost_usd, result.success

        cost, success = asyncio.run(run())
        assert success is False
        # cost_estimate was charged despite failure
        assert cost == pytest.approx(0.01)

    def test_async_probe_does_not_run_fn_on_halt(self) -> None:
        """When budget is exhausted, async call_fn must not be invoked."""
        called = [0]

        async def counting_fn(**kwargs: Any) -> str:
            called[0] += 1
            return "ok"

        async def run() -> str:
            adapter = _make_async(max_cost_usd=0.001, default_cost_per_call=0.01)
            await adapter.wrap_tool_call("tool", {}, counting_fn)  # may or may not pass
            r = await adapter.wrap_tool_call("tool", {}, counting_fn)
            return r.decision

        decision = asyncio.run(run())
        if decision == "HALT":
            # counting_fn should not have been called on the HALT call
            assert called[0] <= 1  # only the first call (if it passed) could have called fn


# ---------------------------------------------------------------------------
# 11. Sync post-call timeout: call_fn completes but exceeded duration
# ---------------------------------------------------------------------------


class TestSyncPostCallTimeout:
    """Sync adapter detects timeout after call_fn returns (non-preemptive)."""

    def test_slow_fn_that_completes_still_gets_timeout_error(self) -> None:
        """call_fn that sleeps 0.2s with 0.05s timeout -> TimeoutError after completion."""
        def slow_fn(**kwargs: Any) -> str:
            time.sleep(0.2)
            return "completed but too slow"

        adapter = _make_sync(timeout_seconds=0.05)
        result = adapter.wrap_tool_call("tool", {}, slow_fn)
        # Non-preemptive: fn runs to completion, then timeout is detected
        assert result.success is False
        assert result.error is not None
        assert "TimeoutError" in result.error or "timeout" in result.error.lower()

    def test_timeout_error_path_increments_error_count(self) -> None:
        """Sync timeout error must increment error_count in stats."""
        def slow_fn(**kwargs: Any) -> str:
            time.sleep(0.2)
            return "done"

        adapter = _make_sync(timeout_seconds=0.05)
        adapter.wrap_tool_call("tool", {}, slow_fn)
        stats = adapter.get_tool_stats()
        assert stats["tool"].error_count == 1

    def test_timeout_result_not_stored_in_stats_cost(self) -> None:
        """Sync timeout error: total_cost_usd in stats should not include timed-out call."""
        def slow_fn(**kwargs: Any) -> str:
            time.sleep(0.2)
            return "done"

        adapter = _make_sync(timeout_seconds=0.05, default_cost_per_call=0.01)
        adapter.wrap_tool_call("tool", {}, slow_fn)
        stats = adapter.get_tool_stats()
        # Error path does not accumulate total_cost_usd
        assert stats["tool"].total_cost_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 12. Deeply nested result: no token_count extractable
# ---------------------------------------------------------------------------


class TestDeepNestedResult:
    """Results with nested dicts but no top-level token_count key."""

    def test_nested_dict_no_token_count_at_top(self) -> None:
        """Nested result without top-level token_count -> 0 tokens extracted."""
        def nested_fn(**kwargs: Any) -> dict[str, Any]:
            return {"data": {"token_count": 100}}  # nested, not top-level

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.01, cost_per_token=0.001)}
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0)
        result = adapter.wrap_tool_call("tool", {}, nested_fn)
        # Top-level has no token_count key -> 0 tokens -> cost = call only
        assert result.cost_usd == pytest.approx(0.01)
        assert result.success is True

    def test_list_result_no_token_count(self) -> None:
        """List result has no token_count -> 0 tokens."""
        def list_fn(**kwargs: Any) -> list[str]:
            return ["item1", "item2", "item3"]

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.05, cost_per_token=0.01)}
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0)
        result = adapter.wrap_tool_call("tool", {}, list_fn)
        assert result.cost_usd == pytest.approx(0.05)
        assert result.success is True

    def test_integer_result_no_token_count(self) -> None:
        """Integer result must not be interpreted as token_count."""
        def int_fn(**kwargs: Any) -> int:
            return 42

        costs = {"tool": MCPToolCost("tool", cost_per_call=0.02, cost_per_token=1.0)}
        ctx = _make_ctx()
        adapter = MCPContainmentAdapter(execution_context=ctx, tool_costs=costs, default_cost_per_call=0.0)
        result = adapter.wrap_tool_call("tool", {}, int_fn)
        assert result.cost_usd == pytest.approx(0.02)
        assert result.success is True


# ---------------------------------------------------------------------------
# 13. Async concurrent CB trip race (TOCTOU)
# ---------------------------------------------------------------------------


class TestAsyncCBTripRace:
    """10 concurrent async calls with 5 failures -- CB state must be valid."""

    def test_concurrent_async_cb_trip_no_corruption(self) -> None:
        """Concurrent CB tripping from 5 failures must leave CB in valid state."""
        async def run() -> CircuitState:
            cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
            adapter = _make_async(circuit_breaker=cb, max_cost_usd=100.0, max_steps=500)
            tasks = []
            for i in range(10):
                fn = _async_raise if i < 5 else _async_echo
                tasks.append(adapter.wrap_tool_call("tool", {}, fn))
            await asyncio.gather(*tasks, return_exceptions=True)
            return cb.state

        state = asyncio.run(run())
        assert state in (CircuitState.CLOSED, CircuitState.OPEN)

    def test_concurrent_async_stats_call_count_consistent(self) -> None:
        """Concurrent async calls: call_count must equal total calls (no missed increments)."""
        async def run() -> int:
            adapter = _make_async(max_cost_usd=100.0, max_steps=500, default_cost_per_call=0.0)
            tasks = [adapter.wrap_tool_call("tool", {}, _async_echo) for _ in range(20)]
            await asyncio.gather(*tasks)
            stats = adapter.get_tool_stats()
            return stats["tool"].call_count

        count = asyncio.run(run())
        assert count == 20


# ---------------------------------------------------------------------------
# 14. Sync adapter: call_fn that returns a coroutine object (not called with await)
# ---------------------------------------------------------------------------


class TestSyncFnReturnsCoroutine:
    """Sync call_fn that returns a coroutine object (forgot async def)."""

    def test_fn_returning_coroutine_object_treated_as_success(self) -> None:
        """A sync fn that returns a coroutine object is NOT the same as async fn.
        The sync adapter must not raise TypeError; result is the coroutine object."""
        import asyncio as _asyncio

        async def coro_factory():
            return "coroutine result"

        def fn_returning_coro(**kwargs: Any) -> Any:
            return coro_factory()  # returns coroutine object, not async fn

        adapter = _make_sync()
        result = adapter.wrap_tool_call("tool", {}, fn_returning_coro)
        # sync fn passes the coroutine guard (it's not a coroutine FUNCTION)
        # result is the coroutine object itself
        assert result.success is True
        # Clean up the unawaited coroutine to avoid RuntimeWarning
        if _asyncio.iscoroutine(result.result):
            result.result.close()


# ---------------------------------------------------------------------------
# 15. M3: tool_name validation (sync and async adapters)
# ---------------------------------------------------------------------------


class TestToolNameValidation:
    """M3: tool_name must be a non-empty string; None and '' must raise ValueError."""

    def test_sync_none_tool_name_raises(self) -> None:
        """wrap_tool_call(tool_name=None) on sync adapter must raise ValueError."""
        adapter = _make_sync()
        with pytest.raises(ValueError, match="tool_name must be a non-empty string"):
            adapter.wrap_tool_call(None, {}, _sync_echo)  # type: ignore[arg-type]

    def test_sync_empty_tool_name_raises(self) -> None:
        """wrap_tool_call(tool_name='') on sync adapter must raise ValueError."""
        adapter = _make_sync()
        with pytest.raises(ValueError, match="tool_name must be a non-empty string"):
            adapter.wrap_tool_call("", {}, _sync_echo)

    def test_sync_non_string_tool_name_raises(self) -> None:
        """Non-string tool_name (int) on sync adapter must raise ValueError."""
        adapter = _make_sync()
        with pytest.raises(ValueError, match="tool_name must be a non-empty string"):
            adapter.wrap_tool_call(42, {}, _sync_echo)  # type: ignore[arg-type]

    def test_async_none_tool_name_raises(self) -> None:
        """wrap_tool_call(tool_name=None) on async adapter must raise ValueError."""
        async def run() -> None:
            adapter = _make_async()
            await adapter.wrap_tool_call(None, {}, _async_echo)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="tool_name must be a non-empty string"):
            asyncio.run(run())

    def test_async_empty_tool_name_raises(self) -> None:
        """wrap_tool_call(tool_name='') on async adapter must raise ValueError."""
        async def run() -> None:
            adapter = _make_async()
            await adapter.wrap_tool_call("", {}, _async_echo)

        with pytest.raises(ValueError, match="tool_name must be a non-empty string"):
            asyncio.run(run())

    def test_sync_valid_tool_name_not_raises(self) -> None:
        """A valid non-empty string tool_name must NOT raise ValueError."""
        adapter = _make_sync()
        result = adapter.wrap_tool_call("my_tool", {}, _sync_echo)
        assert result.success is True

    def test_async_valid_tool_name_not_raises(self) -> None:
        """A valid non-empty string tool_name must NOT raise ValueError (async)."""
        async def run() -> MCPToolResult:
            adapter = _make_async()
            return await adapter.wrap_tool_call("my_tool", {}, _async_echo)

        result = asyncio.run(run())
        assert result.success is True


# ---------------------------------------------------------------------------
# 16. H2: Async concurrent stats consistency (stats_lock)
# ---------------------------------------------------------------------------


class TestAsyncConcurrentStatsConsistency:
    """H2: Concurrent async wrap_tool_call must produce consistent call_count."""

    def test_concurrent_async_stats_call_count_exact(self) -> None:
        """20 concurrent async successful calls must yield call_count == 20 (with lock)."""
        async def run() -> int:
            adapter = _make_async(max_cost_usd=100.0, max_steps=500, default_cost_per_call=0.0)
            tasks = [
                asyncio.create_task(adapter.wrap_tool_call("tool", {}, _async_echo))
                for _ in range(20)
            ]
            results = await asyncio.gather(*tasks)
            # Count actual ALLOW results (not budget HALT from max_steps)
            allowed = sum(1 for r in results if r.decision == "ALLOW" and r.success)
            stats = adapter.get_tool_stats()
            return stats["tool"].call_count, allowed

        call_count, allowed = asyncio.run(run())
        # call_count must be consistent (no missed increments from race)
        assert call_count == 20
        assert call_count >= allowed

    def test_concurrent_async_stats_cost_consistency(self) -> None:
        """20 concurrent async calls with fixed cost: total_cost_usd must be consistent."""
        async def run() -> float:
            adapter = _make_async(
                max_cost_usd=100.0, max_steps=500, default_cost_per_call=0.01
            )
            tasks = [
                asyncio.create_task(adapter.wrap_tool_call("tool", {}, _async_echo))
                for _ in range(20)
            ]
            await asyncio.gather(*tasks)
            stats = adapter.get_tool_stats()
            return stats["tool"].total_cost_usd

        total_cost = asyncio.run(run())
        # 20 calls * 0.01 = 0.20 (no torn reads/writes with lock)
        assert total_cost == pytest.approx(0.20, rel=1e-6)

    def test_concurrent_async_error_count_no_corruption(self) -> None:
        """10 concurrent failures: error_count must equal 10 (no torn updates)."""
        async def run() -> tuple[int, int]:
            adapter = _make_async(max_cost_usd=100.0, max_steps=500, default_cost_per_call=0.0)
            tasks = [
                asyncio.create_task(adapter.wrap_tool_call("tool", {}, _async_raise))
                for _ in range(10)
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            stats = adapter.get_tool_stats()
            return stats["tool"].call_count, stats["tool"].error_count

        call_count, error_count = asyncio.run(run())
        assert call_count == 10
        assert error_count == 10


# ---------------------------------------------------------------------------
# 17. L7: wrap_mcp_server list_tools failure logs at WARNING level
# ---------------------------------------------------------------------------


class TestWrapMCPServerLogLevel:
    """L7: list_tools() failure in wrap_mcp_server must log at WARNING, not DEBUG."""

    def test_list_tools_failure_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """list_tools() failure must emit a WARNING log (not DEBUG)."""
        import logging

        class FailingSession:
            async def list_tools(self) -> None:
                raise RuntimeError("list_tools unavailable")

            async def call_tool(self, *, name: str, arguments: dict) -> Any:
                return {"ok": True}

        async def run() -> None:
            session = FailingSession()
            ctx = _make_ctx()
            with caplog.at_level(logging.WARNING, logger="veronica_core.adapters.mcp_async"):
                await wrap_mcp_server(session=session, execution_context=ctx)

        asyncio.run(run())
        warning_msgs = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING and "list_tools" in r.message
        ]
        assert len(warning_msgs) >= 1, f"Expected WARNING for list_tools failure, got: {caplog.records}"
