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
    if result.decision == "HALT":
        # budget exceeded or circuit open
        handle_halt(result.error)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from veronica_core.circuit_breaker import CircuitBreaker
from veronica_core.containment.execution_context import ExecutionContext, WrapOptions
from veronica_core.runtime_policy import PolicyContext
from veronica_core.shield.types import Decision

logger = logging.getLogger(__name__)

__all__ = [
    "MCPToolCost",
    "MCPToolResult",
    "MCPToolStats",
    "MCPContainmentAdapter",
]


@dataclass(frozen=True)
class MCPToolCost:
    """Cost configuration for an MCP tool.

    Attributes:
        tool_name: Name of the MCP tool (must match argument passed to
            wrap_tool_call).
        cost_per_call: Fixed USD cost charged on each invocation regardless
            of tokens used.
        cost_per_token: Variable USD cost charged per token reported by the
            call result (requires the call result to expose a ``token_count``
            attribute or dict key).
    """

    tool_name: str
    cost_per_call: float = 0.0
    cost_per_token: float = 0.0


@dataclass(frozen=True)
class MCPToolResult:
    """Result of a contained MCP tool call.

    Attributes:
        success: True when call_fn completed without raising.
        result: Value returned by call_fn, or None when blocked/errored.
        error: Human-readable error message, or None on success.
        decision: "ALLOW" when the call was permitted and executed;
            "HALT" when blocked by budget or circuit breaker;
            "DEGRADE" reserved for future degradation-ladder support.
        cost_usd: Actual USD cost charged for this call.
    """

    success: bool
    result: Any = None
    error: Optional[str] = None
    decision: str = "ALLOW"
    cost_usd: float = 0.0


@dataclass
class MCPToolStats:
    """Per-tool usage statistics.

    Attributes:
        tool_name: Name of the tool.
        call_count: Total invocations attempted (including blocked ones).
        total_cost_usd: Cumulative cost across all successful invocations.
        error_count: Number of invocations that raised an exception.
        avg_duration_ms: Rolling average duration of successful invocations.
    """

    tool_name: str
    call_count: int = 0
    total_cost_usd: float = 0.0
    error_count: int = 0
    avg_duration_ms: float = 0.0

    # Internal tracking; not part of the public summary.
    _total_duration_ms: float = field(default=0.0, repr=False)


def _extract_token_count(result: Any) -> int:
    """Extract token count from a call result, returning 0 if not found."""
    if result is None:
        return 0
    if isinstance(result, dict):
        for key in ("token_count", "tokens", "total_tokens", "usage"):
            value = result.get(key)
            if isinstance(value, int) and value >= 0:
                return value
    count = getattr(result, "token_count", None)
    if isinstance(count, int) and count >= 0:
        return count
    return 0


class MCPContainmentAdapter:
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
    """

    def __init__(
        self,
        execution_context: ExecutionContext,
        tool_costs: Optional[dict[str, MCPToolCost]] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        default_cost_per_call: float = 0.001,
    ) -> None:
        if default_cost_per_call < 0:
            raise ValueError("default_cost_per_call must be >= 0")
        self._ctx = execution_context
        self._tool_costs: dict[str, MCPToolCost] = tool_costs or {}
        self._circuit_breaker = circuit_breaker
        self._default_cost_per_call = default_cost_per_call

        # Per-tool stats; keyed by tool_name.
        self._stats: dict[str, MCPToolStats] = {}
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
        self._ensure_stats(tool_name)

        # Circuit breaker pre-check (per-server, before touching budget).
        if self._circuit_breaker is not None:
            cb_decision = self._circuit_breaker.check(PolicyContext())
            if not cb_decision.allowed:
                logger.debug(
                    "[MCP_ADAPTER] tool=%s blocked by circuit breaker: %s",
                    tool_name,
                    cb_decision.reason,
                )
                with self._stats_lock:
                    self._stats[tool_name].call_count += 1
                return MCPToolResult(
                    success=False,
                    error=f"Circuit breaker open: {cb_decision.reason}",
                    decision="HALT",
                    cost_usd=0.0,
                )

        # Determine cost estimate for this call.
        tool_cost = self._tool_costs.get(tool_name)
        cost_estimate = (
            tool_cost.cost_per_call if tool_cost is not None else self._default_cost_per_call
        )

        # Capture result from inside the wrap context via closure.
        call_result: list[Any] = [None]
        call_error: list[Optional[BaseException]] = [None]
        duration_ms_holder: list[float] = [0.0]

        def _execute() -> None:
            t0 = time.monotonic()
            try:
                call_result[0] = call_fn(**arguments)
            except Exception as exc:  # noqa: BLE001
                call_error[0] = exc
            finally:
                duration_ms_holder[0] = (time.monotonic() - t0) * 1000.0

        # Delegate to ExecutionContext for budget tracking.
        opts = WrapOptions(
            operation_name=f"mcp:{tool_name}",
            cost_estimate_hint=cost_estimate,
        )
        ec_decision = self._ctx.wrap_tool_call(fn=_execute, options=opts)

        # Budget HALT: ExecutionContext rejected the call before _execute ran,
        # or _execute ran but cost_estimate pushed total over the limit.
        if ec_decision == Decision.HALT:
            logger.debug("[MCP_ADAPTER] tool=%s blocked by budget HALT", tool_name)
            with self._stats_lock:
                self._stats[tool_name].call_count += 1
            return MCPToolResult(
                success=False,
                error="Budget limit exceeded",
                decision="HALT",
                cost_usd=0.0,
            )

        with self._stats_lock:
            stats = self._stats[tool_name]
            stats.call_count += 1

        # Handle errors raised inside call_fn.
        if call_error[0] is not None:
            exc = call_error[0]
            logger.debug(
                "[MCP_ADAPTER] tool=%s raised %s: %s",
                tool_name,
                type(exc).__name__,
                exc,
            )
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_failure(error=exc)
            with self._stats_lock:
                self._stats[tool_name].error_count += 1
            return MCPToolResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                decision="ALLOW",
                cost_usd=cost_estimate,
            )

        # Compute variable per-token cost if configured.
        actual_cost = cost_estimate
        if tool_cost is not None and tool_cost.cost_per_token > 0:
            token_count = _extract_token_count(call_result[0])
            actual_cost += token_count * tool_cost.cost_per_token

        # Record success in circuit breaker and stats.
        if self._circuit_breaker is not None:
            self._circuit_breaker.record_success()

        with self._stats_lock:
            stats = self._stats[tool_name]
            stats.total_cost_usd += actual_cost
            prev_total = stats._total_duration_ms
            stats._total_duration_ms = prev_total + duration_ms_holder[0]
            successful_calls = stats.call_count - stats.error_count
            stats.avg_duration_ms = (
                stats._total_duration_ms / successful_calls
                if successful_calls > 0
                else 0.0
            )

        return MCPToolResult(
            success=True,
            result=call_result[0],
            decision="ALLOW",
            cost_usd=actual_cost,
        )

    def get_tool_stats(self) -> dict[str, MCPToolStats]:
        """Return a snapshot of per-tool usage statistics.

        The returned dict is a shallow copy; individual MCPToolStats instances
        are the live objects (do not mutate them).

        Returns:
            Mapping of tool_name -> MCPToolStats.
        """
        with self._stats_lock:
            return dict(self._stats)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_stats(self, tool_name: str) -> None:
        """Create a MCPToolStats entry for tool_name if it does not exist."""
        with self._stats_lock:
            if tool_name not in self._stats:
                self._stats[tool_name] = MCPToolStats(tool_name=tool_name)
