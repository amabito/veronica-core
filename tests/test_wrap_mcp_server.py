"""Tests for veronica_core.adapters.mcp_async.wrap_mcp_server().

Uses asyncio.run() wrappers since pytest-asyncio is not available.
Mock session objects simulate mcp.ClientSession without requiring mcp-sdk.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from veronica_core.adapters.mcp import MCPToolCost, MCPToolResult
from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter, wrap_mcp_server
from veronica_core.circuit_breaker import CircuitBreaker, CircuitState
from veronica_core.containment.execution_context import (
    ExecutionConfig,
    ExecutionContext,
)


# ---------------------------------------------------------------------------
# Mock MCP session
# ---------------------------------------------------------------------------


class _MockTool:
    """Simple tool descriptor."""

    def __init__(self, name: str) -> None:
        self.name = name


class _MockListToolsResult:
    def __init__(self, tools: list[_MockTool]) -> None:
        self.tools = tools


class _MockSession:
    """Mock MCP ClientSession."""

    def __init__(
        self,
        tools: Optional[list[str]] = None,
        call_result: Any = None,
        call_raise: Optional[BaseException] = None,
        has_list_tools: bool = True,
    ) -> None:
        self._tools = [_MockTool(t) for t in (tools or [])]
        self._call_result = call_result if call_result is not None else {"status": "ok"}
        self._call_raise = call_raise
        self._has_list_tools = has_list_tools
        self.call_tool_count = 0
        self.list_tools_count = 0

    async def list_tools(self) -> _MockListToolsResult:
        if not self._has_list_tools:
            raise AttributeError("list_tools")
        self.list_tools_count += 1
        return _MockListToolsResult(self._tools)

    async def call_tool(self, *, name: str, arguments: dict) -> Any:
        self.call_tool_count += 1
        if self._call_raise is not None:
            raise self._call_raise
        return self._call_result


def _make_session(
    tools: Optional[list[str]] = None,
    call_result: Any = None,
    call_raise: Optional[BaseException] = None,
    has_list_tools: bool = True,
) -> _MockSession:
    return _MockSession(
        tools=tools,
        call_result=call_result,
        call_raise=call_raise,
        has_list_tools=has_list_tools,
    )


def _make_ctx(max_cost_usd: float = 10.0, max_steps: int = 100) -> ExecutionContext:
    config = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=max_steps,
        max_retries_total=5,
    )
    return ExecutionContext(config=config)


# ---------------------------------------------------------------------------
# Basic creation
# ---------------------------------------------------------------------------


class TestWrapMCPServerCreation:
    def test_returns_bound_adapter(self) -> None:
        async def run() -> Any:
            session = _make_session(tools=["search"])
            ctx = _make_ctx()
            return await wrap_mcp_server(session=session, execution_context=ctx)

        adapter = asyncio.run(run())
        assert isinstance(adapter, AsyncMCPContainmentAdapter)

    def test_adapter_has_call_tool_method(self) -> None:
        async def run() -> bool:
            session = _make_session(tools=["search"])
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(session=session, execution_context=ctx)
            return hasattr(adapter, "call_tool")

        assert asyncio.run(run()) is True

    def test_list_tools_called_on_creation(self) -> None:
        async def run() -> int:
            session = _make_session(tools=["search", "browse"])
            ctx = _make_ctx()
            await wrap_mcp_server(session=session, execution_context=ctx)
            return session.list_tools_count

        assert asyncio.run(run()) == 1

    def test_no_list_tools_does_not_raise(self) -> None:
        async def run() -> Any:
            session = _make_session(has_list_tools=False)
            ctx = _make_ctx()
            return await wrap_mcp_server(session=session, execution_context=ctx)

        adapter = asyncio.run(run())
        assert adapter is not None

    def test_list_tools_failure_does_not_prevent_creation(self) -> None:
        class BrokenSession(_MockSession):
            async def list_tools(self) -> _MockListToolsResult:
                raise RuntimeError("server down")

        async def run() -> Any:
            session = BrokenSession()
            ctx = _make_ctx()
            return await wrap_mcp_server(session=session, execution_context=ctx)

        adapter = asyncio.run(run())
        assert adapter is not None

    def test_discovered_tools_auto_cost_populated(self) -> None:
        async def run() -> dict:
            session = _make_session(tools=["search", "browse"])
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(session=session, execution_context=ctx)
            return adapter._tool_costs

        costs = asyncio.run(run())
        assert "search" in costs
        assert "browse" in costs

    def test_explicit_costs_take_precedence_over_discovered(self) -> None:
        async def run() -> float:
            session = _make_session(tools=["search"])
            ctx = _make_ctx()
            explicit = {"search": MCPToolCost("search", cost_per_call=0.99)}
            adapter = await wrap_mcp_server(
                session=session, execution_context=ctx, tool_costs=explicit
            )
            return adapter._tool_costs["search"].cost_per_call

        assert asyncio.run(run()) == pytest.approx(0.99)

    def test_allowed_tools_filters_discovered(self) -> None:
        """Only tools in allowed_tools should be registered."""
        async def run() -> dict:
            session = _make_session(tools=["search", "browse", "execute"])
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(
                session=session,
                execution_context=ctx,
                allowed_tools={"search", "browse"},
            )
            return adapter._tool_costs

        costs = asyncio.run(run())
        assert "search" in costs
        assert "browse" in costs
        assert "execute" not in costs

    def test_allowed_tools_none_allows_all(self) -> None:
        """When allowed_tools is None (default), all discovered tools are registered."""
        async def run() -> dict:
            session = _make_session(tools=["search", "browse", "execute"])
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(
                session=session, execution_context=ctx, allowed_tools=None
            )
            return adapter._tool_costs

        costs = asyncio.run(run())
        assert "search" in costs
        assert "browse" in costs
        assert "execute" in costs

    def test_allowed_tools_empty_set_blocks_all(self) -> None:
        """Empty allowed_tools set should block all discovered tools."""
        async def run() -> dict:
            session = _make_session(tools=["search", "browse"])
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(
                session=session, execution_context=ctx, allowed_tools=set()
            )
            return adapter._tool_costs

        costs = asyncio.run(run())
        assert "search" not in costs
        assert "browse" not in costs


# ---------------------------------------------------------------------------
# call_tool delegation
# ---------------------------------------------------------------------------


class TestCallToolDelegation:
    def test_call_tool_delegates_to_session(self) -> None:
        expected = {"results": ["item1"]}

        async def run() -> MCPToolResult:
            session = _make_session(tools=["search"], call_result=expected)
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(session=session, execution_context=ctx)
            return await adapter.call_tool("search", {"query": "hello"})

        result = asyncio.run(run())
        assert result.success is True
        assert result.result == expected

    def test_call_tool_passes_name_and_arguments_to_session(self) -> None:
        captured: list[dict] = []

        class CapturingSession(_MockSession):
            async def call_tool(self, *, name: str, arguments: dict) -> Any:
                captured.append({"name": name, "arguments": arguments})
                return {"ok": True}

        async def run() -> None:
            session = CapturingSession(tools=[])
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(session=session, execution_context=ctx)
            await adapter.call_tool("search", {"query": "test", "limit": 5})

        asyncio.run(run())
        assert len(captured) == 1
        assert captured[0]["name"] == "search"
        assert captured[0]["arguments"] == {"query": "test", "limit": 5}

    def test_call_tool_session_error_returns_failure(self) -> None:
        async def run() -> MCPToolResult:
            session = _make_session(
                tools=["tool"], call_raise=RuntimeError("server error")
            )
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(session=session, execution_context=ctx)
            return await adapter.call_tool("tool", {})

        result = asyncio.run(run())
        assert result.success is False
        assert "tool call failed" in result.error

    def test_call_tool_decision_allow_on_success(self) -> None:
        async def run() -> str:
            session = _make_session(tools=["search"], call_result={"ok": True})
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(session=session, execution_context=ctx)
            r = await adapter.call_tool("search", {})
            return r.decision

        assert asyncio.run(run()) == "ALLOW"


# ---------------------------------------------------------------------------
# Containment: budget HALT
# ---------------------------------------------------------------------------


class TestContainmentBudget:
    def test_budget_halt_blocks_call_tool(self) -> None:
        async def run() -> str:
            session = _make_session(tools=["search"], call_result={"ok": True})
            ctx = _make_ctx(max_cost_usd=0.0001)
            adapter = await wrap_mcp_server(
                session=session,
                execution_context=ctx,
                default_cost_per_call=0.01,
            )
            await adapter.call_tool("search", {})
            r = await adapter.call_tool("search", {})
            return r.decision

        assert asyncio.run(run()) == "HALT"

    def test_budget_halt_does_not_call_session(self) -> None:
        async def run() -> tuple[int, str]:
            session = _make_session(tools=["search"], call_result={"ok": True})
            # budget allows exactly 1 call at 0.01 cost
            ctx = _make_ctx(max_cost_usd=0.015)
            adapter = await wrap_mcp_server(
                session=session,
                execution_context=ctx,
                default_cost_per_call=0.01,
            )
            await adapter.call_tool("search", {})
            r2 = await adapter.call_tool("search", {})
            return session.call_tool_count, r2.decision

        count, decision = asyncio.run(run())
        assert decision == "HALT"
        assert count == 1


# ---------------------------------------------------------------------------
# Containment: circuit breaker
# ---------------------------------------------------------------------------


class TestContainmentCircuitBreaker:
    def test_cb_open_blocks_call_tool(self) -> None:
        async def run() -> str:
            session = _make_session(tools=["tool"], call_raise=RuntimeError("flapping"))
            cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(
                session=session, execution_context=ctx, circuit_breaker=cb
            )
            await adapter.call_tool("tool", {})
            await adapter.call_tool("tool", {})
            assert cb.state == CircuitState.OPEN
            # Swap session to return success, but CB is OPEN
            session._call_raise = None
            session._call_result = {"ok": True}
            r = await adapter.call_tool("tool", {})
            return r.decision

        assert asyncio.run(run()) == "HALT"

    def test_successful_calls_keep_cb_closed(self) -> None:
        async def run() -> CircuitState:
            session = _make_session(tools=["tool"], call_result={"ok": True})
            cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(
                session=session, execution_context=ctx, circuit_breaker=cb
            )
            for _ in range(5):
                await adapter.call_tool("tool", {})
            return cb.state

        assert asyncio.run(run()) == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Timeout passthrough
# ---------------------------------------------------------------------------


class TestTimeoutPassthrough:
    def test_timeout_applied_to_call_tool(self) -> None:
        class SlowSession(_MockSession):
            async def call_tool(self, *, name: str, arguments: dict) -> Any:
                await asyncio.sleep(10.0)
                return {"ok": True}

        async def run() -> MCPToolResult:
            session = SlowSession(tools=[])
            ctx = _make_ctx()
            adapter = await wrap_mcp_server(
                session=session, execution_context=ctx, timeout_seconds=0.05
            )
            return await adapter.call_tool("slow_tool", {})

        result = asyncio.run(run())
        assert result.success is False
        assert result.error is not None
        assert "tool call failed" in result.error
