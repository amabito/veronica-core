"""veronica_core.adapters._mcp_base -- Shared base for MCP containment adapters.

Internal module; not part of the public API. Provides the shared dataclasses,
helper utilities, and base class used by both MCPContainmentAdapter (sync) and
AsyncMCPContainmentAdapter (async).

Public types (re-exported from mcp.py and mcp_async.py):
    MCPToolCost       -- cost configuration for a single MCP tool
    MCPToolResult     -- result of a contained MCP tool call
    MCPToolStats      -- per-tool usage statistics
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from veronica_core.circuit_breaker import CircuitBreaker, FailurePredicate
from veronica_core.containment.execution_context import ExecutionContext, WrapOptions
from veronica_core.runtime_policy import PolicyContext

logger = logging.getLogger(__name__)

# Maximum number of distinct tool names tracked in stats before emitting a
# warning.  Does not prevent tracking -- the limit exists to alert operators of
# unbounded tool-name generation (e.g. from attacker-controlled input).
_STATS_WARN_LIMIT = 10_000


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


class _MCPAdapterBase:
    """Shared base for MCPContainmentAdapter and AsyncMCPContainmentAdapter.

    Contains shared __init__ logic, config validation, cost estimation,
    circuit breaker helpers, and get_tool_stats(). The stats dict and lock
    are defined here but the lock *type* (threading.Lock vs asyncio.Lock)
    is set by each subclass after calling super().__init__().

    Subclasses must:
    - Override (or keep) self._stats_lock with the appropriate lock type.
    - Implement wrap_tool_call / wrap_tool_call_async.
    - Implement _ensure_stats (sync or async).
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
        # NOTE: subclasses override self._stats_lock with the appropriate type:
        #   sync:  threading.Lock()
        #   async: asyncio.Lock()
        # This placeholder is overwritten immediately in each subclass __init__.
        self._stats_lock: Any = None

    # ------------------------------------------------------------------
    # Public API (shared)
    # ------------------------------------------------------------------

    def get_tool_stats(self) -> dict[str, MCPToolStats]:
        """Return a snapshot of per-tool usage statistics.

        The returned dict is a shallow copy; individual MCPToolStats instances
        are the live objects (do not mutate them).

        Returns:
            Mapping of tool_name -> MCPToolStats.
        """
        return dict(self._stats)

    # ------------------------------------------------------------------
    # Internal helpers (shared)
    # ------------------------------------------------------------------

    def _compute_cost_estimate(self, tool_name: str) -> float:
        """Return the cost estimate (USD) for the given tool_name."""
        tool_cost = self._tool_costs.get(tool_name)
        return tool_cost.cost_per_call if tool_cost is not None else self._default_cost_per_call

    def _compute_actual_cost(self, tool_name: str, result_value: Any) -> float:
        """Return actual cost including per-token charge if configured."""
        tool_cost = self._tool_costs.get(tool_name)
        cost_estimate = (
            tool_cost.cost_per_call if tool_cost is not None else self._default_cost_per_call
        )
        if tool_cost is not None and tool_cost.cost_per_token > 0:
            token_count = _extract_token_count(result_value)
            cost_estimate += token_count * tool_cost.cost_per_token
        return cost_estimate

    def _build_wrap_options(self, tool_name: str, cost_estimate: float) -> WrapOptions:
        """Build WrapOptions for ExecutionContext.wrap_tool_call."""
        return WrapOptions(
            operation_name=f"mcp:{tool_name}",
            cost_estimate_hint=cost_estimate,
        )

    def _check_circuit_breaker(self, tool_name: str) -> Optional[MCPToolResult]:
        """Check the circuit breaker. Returns a HALT MCPToolResult if open, else None."""
        if self._circuit_breaker is None:
            return None
        cb_decision = self._circuit_breaker.check(PolicyContext())
        if not cb_decision.allowed:
            logger.debug(
                "[MCP_ADAPTER] tool=%s blocked by circuit breaker: %s",
                tool_name,
                cb_decision.reason,
            )
            return MCPToolResult(
                success=False,
                error=f"Circuit breaker open: {cb_decision.reason}",
                decision="HALT",
                cost_usd=0.0,
            )
        return None

    def _record_circuit_breaker_failure(self, exc: BaseException) -> None:
        """Record a failure in the circuit breaker if applicable."""
        if self._circuit_breaker is None:
            return
        if self._failure_predicate is None or self._failure_predicate(exc):
            self._circuit_breaker.record_failure(error=exc)

    def _record_circuit_breaker_success(self) -> None:
        """Record a success in the circuit breaker if one is configured."""
        if self._circuit_breaker is not None:
            self._circuit_breaker.record_success()
