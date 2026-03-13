"""veronica_core.adapters.mcp -- MCP containment adapter.

Wraps MCP (Model Context Protocol) tool calls with veronica-core budget
and circuit breaker enforcement. Does NOT require the mcp-sdk library.

The adapter wraps arbitrary callables, making it MCP-compatible but not
MCP-specific. Circuit breakers are applied per MCP server; tool costs are
configurable per tool.

Public API:
    MCPToolCost -- cost configuration for a single MCP tool
    MCPToolResult -- result of a contained MCP tool call
    MCPToolStats -- per-tool usage statistics
    MCPContainmentAdapter -- wraps tool calls with budget + circuit breaker

Example::

    from veronica_core.adapters.mcp import MCPContainmentAdapter, MCPToolCost
    from veronica_core.containment import ExecutionConfig, ExecutionContext

    config = ExecutionConfig(max_cost_usd=1.0, max_steps=50, max_retries_total=10)
    ctx = ExecutionContext(config=config)

    adapter = MCPContainmentAdapter(
        execution_context=ctx,
        tool_costs={"web_search": MCPToolCost("web_search", cost_per_call=0.01)},
    )
    result = adapter.wrap_tool_call("web_search", {"query": "hello"}, search_fn)
    if result.decision == Decision.HALT:
        # budget exceeded or circuit open
        handle_halt(result.error)
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from veronica_core.adapter_capabilities import AdapterCapabilities

from veronica_core.adapters._mcp_base import (
    MCPToolCost,
    MCPToolResult,
    MCPToolStats,
    _MCPAdapterBase,
    _STATS_WARN_LIMIT,
    _extract_token_count,  # noqa: F401 -- re-exported for backward compatibility
)
from veronica_core.circuit_breaker import FailurePredicate
from veronica_core.containment.execution_context import ExecutionContext
from veronica_core.shield.types import Decision

logger = logging.getLogger(__name__)

__all__ = [
    "MCPToolCost",
    "MCPToolResult",
    "MCPToolStats",
    "MCPContainmentAdapter",
    "FailurePredicate",
]


class MCPContainmentAdapter(_MCPAdapterBase):
    """Wraps MCP tool calls with veronica-core containment.

    Enforces budget limits via ExecutionContext.wrap_tool_call() and applies
    an optional per-server circuit breaker. All statistics are tracked
    per tool and are accessible through get_tool_stats().

    Circuit breakers are per-server (not per-tool): if one MCP server is
    flapping, all tools on that server are blocked, but tools on other
    servers are unaffected.

    Thread-safe: wrap_tool_call() may be called concurrently.

    Args:
        execution_context: Chain-level containment context. Controls budget
            and step limits that span all tool calls within one agent run.
        tool_costs: Mapping of tool_name -> MCPToolCost. Tools not in this
            map use default_cost_per_call.
        circuit_breaker: Optional CircuitBreaker shared across all tools on
            this server. Caller is responsible for not sharing this instance
            across multiple MCPContainmentAdapters (use bind_to_context or
            create separate instances).
        default_cost_per_call: Cost applied to tools without an explicit
            MCPToolCost entry. Must be >= 0.
        timeout_seconds: If set, tool calls that exceed this duration raise
            a TimeoutError stored in MCPToolResult.error. Note: timeout is
            enforced post-hoc -- the tool call runs to completion and the
            timeout is checked after. For preemptive timeout, use
            AsyncMCPContainmentAdapter with asyncio.wait_for().
    """

    def __init__(
        self,
        execution_context: ExecutionContext,
        tool_costs: Optional[dict[str, MCPToolCost]] = None,
        circuit_breaker=None,
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
        self._stats_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def wrap_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call_fn: Callable[..., Any],
    ) -> MCPToolResult:
        """Invoke call_fn under budget and circuit-breaker containment.

        Checks the circuit breaker (if configured) and the budget limit in
        ExecutionContext before invoking call_fn. If either check fails, the
        call is blocked and a HALT result is returned without calling call_fn.

        Cost is computed from the MCPToolCost registered for tool_name (or
        default_cost_per_call) plus a per-token cost if the result exposes a
        token count. The cost is reported to ExecutionContext via
        wrap_tool_call(options.cost_estimate_hint) so it is tracked against
        the chain-level budget.

        Args:
            tool_name: Name of the MCP tool being invoked.
            arguments: Tool arguments dict (not used by containment; passed
                to call_fn via **kwargs).
            call_fn: Callable to invoke. Receives **arguments as keyword args.

        Returns:
            MCPToolResult with success/failure, the raw result, decision, and
            cost charged for this invocation.
        """
        if not tool_name or not isinstance(tool_name, str):
            raise ValueError(f"tool_name must be a non-empty string, got {tool_name!r}")

        if inspect.iscoroutinefunction(call_fn):
            raise TypeError(
                "call_fn is a coroutine function; use AsyncMCPContainmentAdapter instead"
            )

        self._ensure_stats(tool_name)

        # Circuit breaker pre-check (per-server, before touching budget).
        halt_result = self._check_circuit_breaker(tool_name)
        if halt_result is not None:
            self._increment_call_count(tool_name)
            return halt_result

        # Determine cost estimate for this call.
        cost_estimate = self._compute_cost_estimate(tool_name)

        # Capture result from inside the wrap context via nonlocal closure.
        call_result: Any = None
        call_error: Optional[BaseException] = None
        call_duration_ms: float = 0.0

        def _execute() -> None:
            nonlocal call_result, call_error, call_duration_ms
            t0 = time.monotonic()
            try:
                call_result = call_fn(**arguments)
            except Exception as exc:  # noqa: BLE001
                call_error = exc
            finally:
                call_duration_ms = (time.monotonic() - t0) * 1000.0
                if (
                    call_error is None
                    and self._timeout_seconds is not None
                    and call_duration_ms > self._timeout_seconds * 1000.0
                ):
                    call_error = TimeoutError(
                        f"Tool call exceeded {self._timeout_seconds}s timeout"
                    )

        # Delegate to ExecutionContext for budget tracking.
        opts = self._build_wrap_options(tool_name, cost_estimate)
        ec_decision = self._ctx.wrap_tool_call(fn=_execute, options=opts)

        # Budget HALT: ExecutionContext rejected the call before _execute ran,
        # or _execute ran but cost_estimate pushed total over the limit.
        if ec_decision == Decision.HALT:
            logger.debug("[MCP_ADAPTER] tool=%s blocked by budget HALT", tool_name)
            self._increment_call_count(tool_name)
            return MCPToolResult(
                success=False,
                error="Budget limit exceeded",
                decision=Decision.HALT,
                cost_usd=0.0,
            )

        # Handle errors raised inside call_fn.
        if call_error is not None:
            exc = call_error
            logger.debug(
                "[MCP_ADAPTER] tool=%s raised %s: %s",
                tool_name,
                type(exc).__name__,
                exc,
            )
            self._record_circuit_breaker_failure(exc)
            self._increment_error_count(tool_name)
            return MCPToolResult(
                success=False,
                error="tool call failed",
                decision=Decision.ALLOW,
                cost_usd=cost_estimate,
            )

        # Check if MCP tool itself reported an error via isError flag.
        result_value = call_result
        if getattr(result_value, "isError", False):
            self._increment_error_count(tool_name)
            return MCPToolResult(
                success=False,
                result=result_value,
                error="MCP tool returned isError=True",
                decision=Decision.ALLOW,
                cost_usd=cost_estimate,
            )

        # Compute variable per-token cost if configured.
        actual_cost = self._compute_actual_cost(tool_name, result_value)

        # Report per-token cost delta to ExecutionContext budget.
        # wrap_tool_call() only saw cost_estimate (= cost_per_call); the
        # per-token component was unknown until after call_fn returned.
        token_delta = actual_cost - cost_estimate
        if token_delta > 0:
            self._ctx._budget_backend.add(token_delta)
            self._ctx._limits.budget.add(token_delta)

        # Record success in circuit breaker and stats.
        self._record_circuit_breaker_success()

        with self._stats_lock:
            stats = self._stats.get(tool_name)
            if stats is not None:
                stats.call_count += 1
                stats.total_cost_usd += actual_cost
                stats._total_duration_ms += call_duration_ms
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
            supports_reserve_commit=True,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _increment_call_count(self, tool_name: str) -> None:
        """Safely increment call_count, tolerating missing stats entries."""
        with self._stats_lock:
            stats = self._stats.get(tool_name)
            if stats is not None:
                stats.call_count += 1

    def _increment_error_count(self, tool_name: str) -> None:
        """Safely increment both call_count and error_count."""
        with self._stats_lock:
            stats = self._stats.get(tool_name)
            if stats is not None:
                stats.call_count += 1
                stats.error_count += 1

    def _ensure_stats(self, tool_name: str) -> None:
        """Create a MCPToolStats entry for tool_name if it does not exist."""
        if tool_name in self._stats:  # fast path: no lock needed for read under GIL
            return
        with self._stats_lock:
            if tool_name not in self._stats:  # double-check after acquiring lock
                if len(self._stats) >= _STATS_WARN_LIMIT:
                    logger.warning(
                        "MCP adapter stats tracking %d+ distinct tool names; "
                        "dropping new tool name to prevent DoS",
                        _STATS_WARN_LIMIT,
                    )
                    return
                self._stats[tool_name] = MCPToolStats(tool_name=tool_name)
