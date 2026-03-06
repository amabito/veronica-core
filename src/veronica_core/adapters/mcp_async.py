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
    if result.decision == Decision.HALT:
        handle_halt(result.error)
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from veronica_core.adapter_capabilities import AdapterCapabilities

from veronica_core.adapters._mcp_base import (
    MCPToolCost,
    MCPToolResult,
    MCPToolStats,
    _MCPAdapterBase,
    _STATS_WARN_LIMIT,
    _extract_token_count,  # noqa: F401 — re-exported for backward compatibility
)
from veronica_core.circuit_breaker import CircuitBreaker, FailurePredicate
from veronica_core.containment.execution_context import ExecutionContext
from veronica_core.shield.types import Decision

logger = logging.getLogger(__name__)

__all__ = ["AsyncMCPContainmentAdapter", "wrap_mcp_server"]

# Type alias for async tool callables.
AsyncCallFn = Callable[..., Awaitable[Any]]


class AsyncMCPContainmentAdapter(_MCPAdapterBase):
    """Async version of MCPContainmentAdapter.

    Wraps async MCP tool calls with veronica-core containment. All semantics
    mirror the sync adapter except:

    - ``call_fn`` must be an async callable (``async def``); it is awaited.
    - ``timeout_seconds`` adds an ``asyncio.wait_for`` timeout around the call.
    - ``failure_predicate`` restricts which exceptions trip the circuit breaker.
    - Stats mutations are protected by ``self._stats_lock`` (asyncio.Lock) to prevent
      interleaving between coroutines that resume after ``await call_fn()``.

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
        super().__init__(
            execution_context=execution_context,
            tool_costs=tool_costs,
            circuit_breaker=circuit_breaker,
            default_cost_per_call=default_cost_per_call,
            timeout_seconds=timeout_seconds,
            failure_predicate=failure_predicate,
        )
        # Per-tool stats; keyed by tool_name.
        # Lock required: stats updates occur after `await call_fn()`, so two
        # coroutines can interleave and produce torn reads/writes without locking.
        self._stats_lock = asyncio.Lock()
        # Cache backend reserve capability once (doesn't change after init).
        # True when the backend exposes reserve/commit/rollback (sync or async).
        # At call time we further check iscoroutinefunction to decide whether
        # to await the result.
        _backend = getattr(self._ctx, "_budget_backend", None)
        self._backend_supports_reserve: bool = _backend is not None and hasattr(
            _backend, "reserve"
        )
        # Cache whether reserve is async so we avoid repeated inspection per call.
        self._reserve_is_async: bool = (
            _backend is not None
            and inspect.iscoroutinefunction(getattr(_backend, "reserve", None))
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_tool_stats_async(self) -> dict[str, MCPToolStats]:
        """Return an immutable snapshot of per-tool usage statistics (async-safe).

        Async adapters use asyncio.Lock for _stats_lock, so get_tool_stats()
        from the base class (which uses the synchronous ``with`` statement) would
        deadlock.  Use this coroutine instead.

        Returns:
            Mapping of tool_name -> MCPToolStats snapshot.
        """
        async with self._stats_lock:
            return {
                name: dataclasses.replace(stats) for name, stats in self._stats.items()
            }

    def get_tool_stats(self) -> dict[str, MCPToolStats]:
        """Synchronous snapshot — safe only when called from a non-async context.

        Callers inside an asyncio event loop should use ``get_tool_stats_async()``
        instead.  This override avoids attempting to acquire an asyncio.Lock from
        synchronous code, which would raise RuntimeError.

        Returns a best-effort snapshot: it captures the dict items at one point in
        time without holding the lock.  Concurrent mutations cannot corrupt the dict
        reference itself, but individual MCPToolStats copies may reflect a mix of
        before- and after-mutation state.  Acceptable for monitoring/debug callers
        that do not rely on exact atomicity.
        """
        # Take a stable list of items without holding an async lock from sync code.
        items = list(self._stats.items())
        return {name: dataclasses.replace(stats) for name, stats in items}

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
        if not tool_name or not isinstance(tool_name, str):
            raise ValueError(f"tool_name must be a non-empty string, got {tool_name!r}")

        await self._ensure_stats(tool_name)

        # Circuit breaker pre-check.
        halt_result = self._check_circuit_breaker(tool_name)
        if halt_result is not None:
            async with self._stats_lock:
                self._stats[tool_name].call_count += 1
            return halt_result

        # Determine cost estimate.
        cost_estimate = self._compute_cost_estimate(tool_name)

        # Budget gate: use reserve/commit/rollback when available (two-phase
        # atomicity), otherwise fall back to the sync _budget_probe no-op.
        budget_backend = getattr(self._ctx, "_budget_backend", None)
        _use_reserve = cost_estimate > 0.0 and self._backend_supports_reserve
        _reservation_id: Optional[str] = None

        if _use_reserve:
            # Phase 1a: reserve the cost estimate against the ceiling.
            # Support both sync and async reserve implementations.
            try:
                _reserve_result = budget_backend.reserve(
                    cost_estimate, self._ctx._config.max_cost_usd
                )
                _reservation_id = (
                    await _reserve_result
                    if self._reserve_is_async
                    else _reserve_result
                )
            except OverflowError:
                logger.debug(
                    "[ASYNC_MCP_ADAPTER] tool=%s blocked by budget HALT (reserve)",
                    tool_name,
                )
                async with self._stats_lock:
                    self._stats[tool_name].call_count += 1
                return MCPToolResult(
                    success=False,
                    error="Budget limit exceeded",
                    decision=Decision.HALT,
                    cost_usd=0.0,
                )
        else:
            # Fallback: sync budget probe via ExecutionContext.wrap_tool_call.
            def _budget_probe() -> None:
                pass

            opts = self._build_wrap_options(tool_name, cost_estimate)
            ec_decision = self._ctx.wrap_tool_call(fn=_budget_probe, options=opts)

            if ec_decision == Decision.HALT:
                logger.debug(
                    "[ASYNC_MCP_ADAPTER] tool=%s blocked by budget HALT", tool_name
                )
                async with self._stats_lock:
                    self._stats[tool_name].call_count += 1
                return MCPToolResult(
                    success=False,
                    error="Budget limit exceeded",
                    decision=Decision.HALT,
                    cost_usd=0.0,
                )

        # Phase 2: await call_fn.
        call_error: Optional[BaseException] = None
        result_value: Any = None
        t0 = time.monotonic()
        try:
            if self._timeout_seconds is not None:
                result_value = await asyncio.wait_for(
                    call_fn(**arguments), timeout=self._timeout_seconds
                )
            else:
                result_value = await call_fn(**arguments)
        except Exception as exc:  # noqa: BLE001
            call_error = exc
        finally:
            duration_ms = (time.monotonic() - t0) * 1000.0

        # Handle errors raised inside call_fn.
        if call_error is not None:
            logger.debug(
                "[ASYNC_MCP_ADAPTER] tool=%s raised %s: %s",
                tool_name,
                type(call_error).__name__,
                call_error,
            )
            if _reservation_id is not None:
                try:
                    _rb = budget_backend.rollback(_reservation_id)
                    if self._reserve_is_async:
                        await _rb
                except Exception:  # noqa: BLE001
                    pass
            self._record_circuit_breaker_failure(call_error)
            async with self._stats_lock:
                self._stats[tool_name].call_count += 1
                self._stats[tool_name].error_count += 1
            return MCPToolResult(
                success=False,
                error=f"{type(call_error).__name__}: {call_error}",
                decision=Decision.ALLOW,
                cost_usd=cost_estimate,
            )

        # isError detection: application-level error, not a CB trip.
        if getattr(result_value, "isError", False):
            logger.debug("[ASYNC_MCP_ADAPTER] tool=%s returned isError=True", tool_name)
            if _reservation_id is not None:
                try:
                    _rb = budget_backend.rollback(_reservation_id)
                    if self._reserve_is_async:
                        await _rb
                except Exception:  # noqa: BLE001
                    pass
            async with self._stats_lock:
                self._stats[tool_name].call_count += 1
                self._stats[tool_name].error_count += 1
            return MCPToolResult(
                success=False,
                result=result_value,
                error="Tool returned isError=True",
                decision=Decision.ALLOW,
                cost_usd=cost_estimate,
            )

        # Compute variable per-token cost.
        actual_cost = self._compute_actual_cost(tool_name, result_value)

        # Phase 3: commit the reservation or record cost on legacy path.
        if _reservation_id is not None:
            try:
                _cm = budget_backend.commit(_reservation_id)
                if self._reserve_is_async:
                    await _cm
            except Exception as _commit_exc:  # noqa: BLE001
                # Commit failed (expired, already committed, or backend error).
                # Do NOT call add() — that would double-charge the budget if the
                # reservation was already flushed.  Accept the under-count and log.
                logger.warning(
                    "[ASYNC_MCP_ADAPTER] tool=%s commit(%s) failed: %s — cost may be untracked",
                    tool_name,
                    _reservation_id,
                    _commit_exc,
                )
        else:
            # Legacy path: _budget_probe was a no-op, so actual cost was never
            # recorded against the budget.  Charge it now via wrap_tool_call with
            # the actual cost so that the ExecutionContext tracks the spend.
            if actual_cost > 0.0:
                _actual_cost_hint = actual_cost

                def _record_actual_cost() -> None:
                    pass

                opts = self._build_wrap_options(tool_name, _actual_cost_hint)
                self._ctx.wrap_tool_call(fn=_record_actual_cost, options=opts)

        # Record success in CB and stats.
        self._record_circuit_breaker_success()

        async with self._stats_lock:
            stats = self._stats[tool_name]
            stats.call_count += 1
            stats.total_cost_usd += actual_cost
            stats._total_duration_ms += duration_ms
            successful_calls = stats.call_count - stats.error_count
            stats.avg_duration_ms = (
                stats._total_duration_ms / successful_calls
                if successful_calls > 0
                else 0.0
            )

        return MCPToolResult(
            success=True,
            result=result_value,
            decision=Decision.ALLOW,
            cost_usd=actual_cost,
        )

    def capabilities(self) -> "AdapterCapabilities":
        """Return the capability descriptor for this adapter."""
        from veronica_core.adapter_capabilities import AdapterCapabilities

        return AdapterCapabilities(
            framework_name="MCP",
            supports_async=True,
            supports_reserve_commit=True,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_stats(self, tool_name: str) -> None:
        """Create a MCPToolStats entry for tool_name if it does not exist."""
        if tool_name in self._stats:  # fast path: skip lock for existing tools
            return
        async with self._stats_lock:
            if tool_name not in self._stats:
                if len(self._stats) >= _STATS_WARN_LIMIT:
                    logger.warning(
                        "Async MCP adapter stats tracking %d+ distinct tool names; "
                        "this may indicate unbounded tool-name generation",
                        _STATS_WARN_LIMIT,
                    )
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
    but does NOT import ``mcp`` directly -- ``session`` is typed as ``Any`` to
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
            logger.warning(
                "[wrap_mcp_server] list_tools() failed; proceeding without discovery"
            )

    return _BoundMCPAdapter(
        session=session,
        execution_context=execution_context,
        tool_costs=resolved_costs,
        circuit_breaker=circuit_breaker,
        default_cost_per_call=default_cost_per_call,
        timeout_seconds=timeout_seconds,
    )
