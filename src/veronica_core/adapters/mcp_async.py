"""veronica_core.adapters.mcp_async -- Async MCP containment adapter.

Async mirror of MCPContainmentAdapter. Wraps async MCP tool callables with
veronica-core budget and circuit breaker enforcement.

Does NOT require the mcp-sdk library. The adapter wraps arbitrary async
callables, making it MCP-compatible but not MCP-specific.

Public API:
    AsyncMCPContainmentAdapter -- wraps async tool calls with budget + CB

Reuses from .mcp:
    MCPToolCost, MCPToolResult, MCPToolStats

Example::

    from veronica_core.adapters.mcp import MCPToolCost
    from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter
    from veronica_core.containment import ExecutionConfig, ExecutionContext

    config = ExecutionConfig(max_cost_usd=1.0, max_steps=50, max_retries_total=10)
    ctx = ExecutionContext(config=config)

    adapter = AsyncMCPContainmentAdapter(
        execution_context=ctx,
        tool_costs={"web_search": MCPToolCost("web_search", cost_per_call=0.01)},
        timeout_seconds=30.0,
    )

    async def my_search(**kwargs):
        return {"results": [...]}

    result = await adapter.wrap_tool_call("web_search", {"query": "hello"}, my_search)
    if result.decision == "HALT":
        handle_halt(result.error)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from veronica_core.adapters.mcp import (
    MCPToolCost,
    MCPToolResult,
    MCPToolStats,
    _extract_token_count,
)
from veronica_core.circuit_breaker import CircuitBreaker, FailurePredicate
from veronica_core.containment.execution_context import ExecutionContext, WrapOptions
from veronica_core.runtime_policy import PolicyContext
from veronica_core.shield.types import Decision

logger = logging.getLogger(__name__)

__all__ = ["AsyncMCPContainmentAdapter", "wrap_mcp_server"]

# Type alias for async tool callables.
AsyncCallFn = Callable[..., Awaitable[Any]]


class AsyncMCPContainmentAdapter:
    """Async version of MCPContainmentAdapter.

    Wraps async MCP tool calls with veronica-core containment. All semantics
    mirror the sync adapter except:

    - ``call_fn`` must be an async callable (``async def``); it is awaited.
    - ``timeout_seconds`` adds an ``asyncio.wait_for`` timeout around the call.
    - ``failure_predicate`` restricts which exceptions trip the circuit breaker.
    - Stats are protected with ``asyncio.Lock`` instead of ``threading.Lock``.

    Args:
        execution_context: Chain-level containment context. Controls budget
            and step limits that span all tool calls within one agent run.
        tool_costs: Mapping of tool_name -> MCPToolCost. Tools not in this
            map use default_cost_per_call.
        circuit_breaker: Optional CircuitBreaker shared across all tools on
            this server.
        default_cost_per_call: Cost applied to tools without an explicit
            MCPToolCost entry. Must be >= 0.
        timeout_seconds: If set, each call_fn invocation is wrapped with
            ``asyncio.wait_for(timeout=timeout_seconds)``. A TimeoutError
            is recorded as a failure.
        failure_predicate: If set, only exceptions for which the predicate
            returns True will trip the circuit breaker. Exceptions that
            return False are still recorded as tool errors but do not
            increment the CB failure count.
    """

    def __init__(
        self,
        execution_context: ExecutionContext,
        tool_costs: Optional[dict[str, MCPToolCost]] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        default_cost_per_call: float = 0.001,
        timeout_seconds: Optional[float] = None,
        failure_predicate: Optional[FailurePredicate] = None,
    ) -> None:
        if default_cost_per_call < 0:
            raise ValueError("default_cost_per_call must be >= 0")
        self._ctx = execution_context
        self._tool_costs: dict[str, MCPToolCost] = tool_costs or {}
        self._circuit_breaker = circuit_breaker
        self._default_cost_per_call = default_cost_per_call
        self._timeout_seconds = timeout_seconds
        self._failure_predicate = failure_predicate

        # Per-tool stats; keyed by tool_name.
        self._stats: dict[str, MCPToolStats] = {}
        self._stats_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def wrap_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call_fn: AsyncCallFn,
    ) -> MCPToolResult:
        """Invoke async call_fn under budget and circuit-breaker containment.

        Checks the circuit breaker (if configured) and budget limit before
        awaiting call_fn. If either check fails, returns a HALT result
        without calling call_fn.

        isError detection: if the result has ``isError=True``, the call is
        recorded as a failure (success=False) but does NOT trip the circuit
        breaker. This mirrors MCP protocol semantics where isError is an
        application-level error, not a transport failure.

        Args:
            tool_name: Name of the MCP tool being invoked.
            arguments: Tool arguments dict passed to call_fn via **kwargs.
            call_fn: Async callable to invoke.

        Returns:
            MCPToolResult with success/failure, raw result, decision, and
            cost charged for this invocation.
        """
        await self._ensure_stats(tool_name)

        # Circuit breaker pre-check.
        if self._circuit_breaker is not None:
            cb_decision = self._circuit_breaker.check(PolicyContext())
            if not cb_decision.allowed:
                logger.debug(
                    "[ASYNC_MCP_ADAPTER] tool=%s blocked by circuit breaker: %s",
                    tool_name,
                    cb_decision.reason,
                )
                async with self._stats_lock:
                    self._stats[tool_name].call_count += 1
                return MCPToolResult(
                    success=False,
                    error=f"Circuit breaker open: {cb_decision.reason}",
                    decision="HALT",
                    cost_usd=0.0,
                )

        # Determine cost estimate.
        tool_cost = self._tool_costs.get(tool_name)
        cost_estimate = (
            tool_cost.cost_per_call if tool_cost is not None else self._default_cost_per_call
        )

        # Two-phase approach: sync budget gate first, then async invocation.
        # ExecutionContext.wrap_tool_call is synchronous; we pass a no-op to
        # perform the budget check, then await call_fn separately.
        def _budget_probe() -> None:
            pass

        opts = WrapOptions(
            operation_name=f"mcp:{tool_name}",
            cost_estimate_hint=cost_estimate,
        )
        ec_decision = self._ctx.wrap_tool_call(fn=_budget_probe, options=opts)

        if ec_decision == Decision.HALT:
            logger.debug("[ASYNC_MCP_ADAPTER] tool=%s blocked by budget HALT", tool_name)
            async with self._stats_lock:
                self._stats[tool_name].call_count += 1
            return MCPToolResult(
                success=False,
                error="Budget limit exceeded",
                decision="HALT",
                cost_usd=0.0,
            )

        # Phase 2: await call_fn.
        call_result: list[Any] = [None]
        call_error: list[Optional[BaseException]] = [None]
        duration_ms_holder: list[float] = [0.0]
        t0 = time.monotonic()
        try:
            if self._timeout_seconds is not None:
                raw_result = await asyncio.wait_for(
                    call_fn(**arguments), timeout=self._timeout_seconds
                )
            else:
                raw_result = await call_fn(**arguments)
            call_result[0] = raw_result
        except Exception as exc:  # noqa: BLE001
            call_error[0] = exc
        finally:
            duration_ms_holder[0] = (time.monotonic() - t0) * 1000.0

        async with self._stats_lock:
            self._stats[tool_name].call_count += 1

        # Handle errors raised inside call_fn.
        if call_error[0] is not None:
            exc = call_error[0]
            logger.debug(
                "[ASYNC_MCP_ADAPTER] tool=%s raised %s: %s",
                tool_name,
                type(exc).__name__,
                exc,
            )
            if self._circuit_breaker is not None:
                should_trip = (
                    self._failure_predicate is None or self._failure_predicate(exc)
                )
                if should_trip:
                    self._circuit_breaker.record_failure(error=exc)
            async with self._stats_lock:
                self._stats[tool_name].error_count += 1
            return MCPToolResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                decision="ALLOW",
                cost_usd=cost_estimate,
            )

        # isError detection: application-level error, not a CB trip.
        result_value = call_result[0]
        if hasattr(result_value, "isError") and result_value.isError:
            logger.debug(
                "[ASYNC_MCP_ADAPTER] tool=%s returned isError=True", tool_name
            )
            async with self._stats_lock:
                self._stats[tool_name].error_count += 1
            return MCPToolResult(
                success=False,
                result=result_value,
                error="Tool returned isError=True",
                decision="ALLOW",
                cost_usd=cost_estimate,
            )

        # Compute variable per-token cost.
        actual_cost = cost_estimate
        if tool_cost is not None and tool_cost.cost_per_token > 0:
            token_count = _extract_token_count(result_value)
            actual_cost += token_count * tool_cost.cost_per_token

        # Record success in CB and stats.
        if self._circuit_breaker is not None:
            self._circuit_breaker.record_success()

        async with self._stats_lock:
            stats = self._stats[tool_name]
            stats.total_cost_usd += actual_cost
            stats._total_duration_ms += duration_ms_holder[0]
            successful_calls = stats.call_count - stats.error_count
            stats.avg_duration_ms = (
                stats._total_duration_ms / successful_calls
                if successful_calls > 0
                else 0.0
            )

        return MCPToolResult(
            success=True,
            result=result_value,
            decision="ALLOW",
            cost_usd=actual_cost,
        )

    async def get_tool_stats(self) -> dict[str, MCPToolStats]:
        """Return a snapshot of per-tool usage statistics.

        Returns:
            Mapping of tool_name -> MCPToolStats.
        """
        async with self._stats_lock:
            return dict(self._stats)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_stats(self, tool_name: str) -> None:
        """Create a MCPToolStats entry for tool_name if it does not exist."""
        async with self._stats_lock:
            if tool_name not in self._stats:
                self._stats[tool_name] = MCPToolStats(tool_name=tool_name)


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


class _BoundMCPAdapter(AsyncMCPContainmentAdapter):
    """AsyncMCPContainmentAdapter with a pre-bound MCP session.

    Exposes ``call_tool(name, arguments)`` that delegates to
    ``session.call_tool(name=name, arguments=arguments)`` under containment.
    """

    def __init__(self, session: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session = session

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Invoke a tool on the bound MCP session under containment.

        Args:
            name: Tool name registered on the MCP server.
            arguments: Tool argument dict.

        Returns:
            MCPToolResult from wrap_tool_call().
        """

        async def _session_call(**kwargs: Any) -> Any:
            return await self._session.call_tool(name=name, arguments=kwargs)

        return await self.wrap_tool_call(name, arguments, _session_call)


async def wrap_mcp_server(
    session: Any,
    execution_context: ExecutionContext,
    tool_costs: Optional[dict[str, MCPToolCost]] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    default_cost_per_call: float = 0.001,
    timeout_seconds: Optional[float] = None,
) -> _BoundMCPAdapter:
    """Create an AsyncMCPContainmentAdapter pre-configured for an MCP server.

    Queries the server for available tools via ``session.list_tools()`` if the
    session exposes that method. Returns a ``_BoundMCPAdapter`` with a
    ``call_tool(name, arguments)`` convenience method.

    Requires ``pip install veronica-core[mcp]`` for the mcp-sdk session object,
    but does NOT import ``mcp`` directly — ``session`` is typed as ``Any`` to
    avoid a hard dependency.

    Args:
        session: MCP ClientSession (or any object with ``call_tool()`` and
            optionally ``list_tools()``).
        execution_context: Chain-level containment context.
        tool_costs: Optional per-tool cost overrides. Tools discovered via
            ``list_tools()`` but not in this map use default_cost_per_call.
        circuit_breaker: Optional circuit breaker for the server.
        default_cost_per_call: Default cost applied to unconfigured tools.
        timeout_seconds: Per-call timeout passed to asyncio.wait_for().

    Returns:
        _BoundMCPAdapter with ``wrap_tool_call()`` and ``call_tool()`` methods.
    """
    # Optionally discover available tools to pre-populate cost map.
    resolved_costs: dict[str, MCPToolCost] = dict(tool_costs or {})
    if hasattr(session, "list_tools"):
        try:
            tools_response = await session.list_tools()
            # mcp-sdk returns ListToolsResult with .tools attribute.
            tools = getattr(tools_response, "tools", None) or tools_response
            for tool in tools:
                tool_name = getattr(tool, "name", None)
                if tool_name and tool_name not in resolved_costs:
                    resolved_costs[tool_name] = MCPToolCost(
                        tool_name=tool_name,
                        cost_per_call=default_cost_per_call,
                    )
        except Exception:  # noqa: BLE001
            # list_tools failure must not prevent adapter creation.
            logger.debug("[wrap_mcp_server] list_tools() failed; proceeding without discovery")

    return _BoundMCPAdapter(
        session=session,
        execution_context=execution_context,
        tool_costs=resolved_costs,
        circuit_breaker=circuit_breaker,
        default_cost_per_call=default_cost_per_call,
        timeout_seconds=timeout_seconds,
    )
