"""veronica_core.adapters.a2a_client -- A2A client containment adapter.

Wraps outbound A2A agent calls with veronica-core budget, circuit breaker,
and identity-based governance enforcement.

Does NOT require the a2a-sdk library. The adapter wraps arbitrary async
callables that satisfy A2AClientProtocol, making it A2A-compatible but
not A2A-specific.

Public API:
    A2AClientContainmentAdapter -- wraps send_message calls with budget + CB
    BoundA2AAdapter             -- pre-bound client variant
    wrap_a2a_agent              -- convenience factory

Example::

    from veronica_core.adapters._a2a_base import A2AClientConfig, A2AMessageCost
    from veronica_core.adapters.a2a_client import BoundA2AAdapter, wrap_a2a_agent
    from veronica_core.containment import ExecutionConfig, ExecutionContext
    from veronica_core.a2a.types import AgentIdentity, TrustLevel

    config = ExecutionConfig(max_cost_usd=5.0, max_steps=100, max_retries_total=20)
    ctx = ExecutionContext(config=config)

    adapter = wrap_a2a_agent(
        client=my_a2a_client,
        execution_context=ctx,
        config=A2AClientConfig(default_cost_per_message=0.01, timeout_seconds=30.0),
        message_costs={"remote-agent": A2AMessageCost("remote-agent", cost_per_message=0.05)},
    )

    identity = AgentIdentity(agent_id="local-agent", origin="local", trust_level=TrustLevel.TRUSTED)
    result = await adapter.send_message("remote-agent", my_message, tenant_id="t1", identity=identity)
    if result.decision == Decision.HALT:
        handle_halt(result.error)
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from veronica_core.adapter_capabilities import AdapterCapabilities

from veronica_core.adapters._a2a_base import (
    A2AClientConfig,
    A2AClientProtocol,
    A2AMessageCost,
    A2AResult,
    A2AStats,
)
from veronica_core.a2a.provenance import A2AIdentityProvenance
from veronica_core.a2a.types import AgentIdentity
from veronica_core.circuit_breaker import CircuitBreaker, FailurePredicate
from veronica_core.containment.execution_context import ExecutionContext
from veronica_core.containment.types import WrapOptions
from veronica_core.runtime_policy import PolicyContext
from veronica_core.shield.types import Decision

logger = logging.getLogger(__name__)

__all__ = ["A2AClientContainmentAdapter", "BoundA2AAdapter", "wrap_a2a_agent"]


def _extract_token_count(response: Any) -> int:
    """Extract token count from an A2A response, returning 0 if not found.

    Handles flat keys (token_count, tokens, total_tokens) and nested
    usage dicts ({"usage": {"total_tokens": N}}).  Returns 0 for
    non-int, negative, or missing values.
    """
    if response is None:
        return 0
    if isinstance(response, dict):
        for key in ("token_count", "tokens", "total_tokens"):
            value = response.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        # Handle nested usage dict (OpenAI-style {"usage": {"total_tokens": N}})
        usage = response.get("usage")
        if isinstance(usage, dict):
            for ukey in ("total_tokens", "token_count", "tokens"):
                uval = usage.get(ukey)
                if isinstance(uval, int) and not isinstance(uval, bool) and uval >= 0:
                    return uval
    count = getattr(response, "token_count", None)
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    return 0


class A2AClientContainmentAdapter:
    """Async A2A client adapter with budget and circuit-breaker containment.

    Wraps outbound send_message calls to remote A2A agents under veronica-core
    governance. All semantics mirror AsyncMCPContainmentAdapter except:

    - The unit of work is a send_message call, not a tool call.
    - Identity (AgentIdentity) and provenance (A2AIdentityProvenance) are
      threaded through each call for governance hooks.
    - Stats are keyed by agent_id. Future versions will scope by tenant_id.
    - timeout_seconds applies an asyncio.wait_for() around client.send_message().

    Args:
        execution_context: Chain-level containment context. Controls budget
            and step limits that span all agent calls within one run.
        config: A2AClientConfig controlling costs, timeouts, and limits.
        circuit_breaker: Optional CircuitBreaker shared across all agent calls.
        message_costs: Mapping of agent_id -> A2AMessageCost. Agents not in
            this map use config.default_cost_per_message.
        failure_predicate: If set, only exceptions for which the predicate
            returns True will trip the circuit breaker.
    """

    def __init__(
        self,
        execution_context: ExecutionContext,
        config: Optional[A2AClientConfig] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        message_costs: Optional[dict[str, A2AMessageCost]] = None,
        failure_predicate: Optional[FailurePredicate] = None,
    ) -> None:
        if execution_context is None:
            raise ValueError("execution_context must not be None")
        self._ctx = execution_context
        self._config = config if config is not None else A2AClientConfig()
        self._circuit_breaker = circuit_breaker
        self._message_costs: dict[str, A2AMessageCost] = message_costs or {}
        self._failure_predicate = failure_predicate

        # Per-agent stats keyed by agent_id.
        # asyncio.Lock required: stats updates occur after await, so two
        # coroutines can interleave without locking.
        self._stats: dict[str, A2AStats] = {}
        self._stats_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_message(
        self,
        agent_id: str,
        message: Any,
        *,
        tenant_id: str,
        identity: AgentIdentity,
        provenance: A2AIdentityProvenance | None = None,
        client: A2AClientProtocol,
    ) -> A2AResult:
        """Send a message to a remote A2A agent under containment.

        Checks the circuit breaker (if configured) and budget limit before
        awaiting client.send_message(). If either check fails, returns a
        HALT result without calling the client.

        Args:
            agent_id: Remote agent identifier. Used as stats key.
            message: A2A message payload to forward to the remote agent.
            tenant_id: Tenant scope for this request.
            identity: Caller's AgentIdentity for governance hooks.
            provenance: Optional verification metadata for the remote agent.
            client: A2A client that satisfies A2AClientProtocol.

        Returns:
            A2AResult with success/failure, response, decision, and cost.
        """
        if not agent_id or not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError(
                f"agent_id must be a non-empty, non-whitespace string, got {agent_id!r}"
            )
        if not tenant_id or not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError(
                f"tenant_id must be a non-empty, non-whitespace string, got {tenant_id!r}"
            )

        await self._ensure_stats(agent_id)

        # Circuit breaker pre-check.
        if self._circuit_breaker is not None:
            cb_decision = self._circuit_breaker.check(PolicyContext())
            if not cb_decision.allowed:
                logger.debug(
                    "[A2A_CLIENT_ADAPTER] agent=%s tenant=%s blocked by circuit breaker: %s",
                    agent_id,
                    tenant_id,
                    cb_decision.reason,
                )
                await self._increment_call_count(agent_id)
                return A2AResult(
                    success=False,
                    error="Agent unavailable",
                    decision=Decision.HALT,
                    cost_usd=0.0,
                )

        # Compute cost estimate.
        msg_cost = self._message_costs.get(agent_id)
        cost_estimate = (
            msg_cost.cost_per_message
            if msg_cost is not None
            else self._config.default_cost_per_message
        )

        # Budget probe via ExecutionContext.wrap_tool_call.
        def _budget_probe() -> None:
            pass

        opts = WrapOptions(
            operation_name=f"a2a:{agent_id}",
            cost_estimate_hint=cost_estimate,
        )
        ec_decision = self._ctx.wrap_tool_call(fn=_budget_probe, options=opts)

        if ec_decision == Decision.HALT:
            logger.debug(
                "[A2A_CLIENT_ADAPTER] agent=%s tenant=%s blocked by budget HALT",
                agent_id,
                tenant_id,
            )
            await self._increment_call_count(agent_id)
            return A2AResult(
                success=False,
                error="Budget limit exceeded",
                decision=Decision.HALT,
                cost_usd=0.0,
            )

        # Execute send_message with optional timeout.
        call_error: Optional[BaseException] = None
        response: Any = None
        t0 = time.monotonic()
        try:
            if self._config.timeout_seconds is not None:
                response = await asyncio.wait_for(
                    client.send_message(message),
                    timeout=self._config.timeout_seconds,
                )
            else:
                response = await client.send_message(message)
        except asyncio.CancelledError:
            raise  # propagate cancellation -- do not treat as a call failure
        except Exception as exc:  # noqa: BLE001
            call_error = exc
        finally:
            latency_ms = (time.monotonic() - t0) * 1000.0

        # Handle call errors -- error message must not contain str(exc) or type name.
        if call_error is not None:
            logger.debug(
                "[A2A_CLIENT_ADAPTER] agent=%s tenant=%s raised %s: %s",
                agent_id,
                tenant_id,
                type(call_error).__name__,
                call_error,
            )
            if self._circuit_breaker is not None:
                if self._failure_predicate is None or self._failure_predicate(call_error):
                    self._circuit_breaker.record_failure(error=call_error)
            await self._increment_error_count(agent_id, cost_usd=cost_estimate)
            return A2AResult(
                success=False,
                error="Agent call failed",
                decision=Decision.ALLOW,
                cost_usd=cost_estimate,
            )

        # Compute actual cost including per-token charge if configured.
        actual_cost = cost_estimate
        if msg_cost is not None and msg_cost.cost_per_token > 0:
            token_count = _extract_token_count(response)
            actual_cost += token_count * msg_cost.cost_per_token

        # Charge per-token delta to ExecutionContext budget.
        # The budget probe (wrap_tool_call) only charged cost_estimate.
        # Any per-token surplus must be added separately to prevent the
        # chain from exceeding its configured cost ceiling.
        token_delta = actual_cost - cost_estimate
        if token_delta > 0:
            budget_backend = getattr(self._ctx, "_budget_backend", None)
            if budget_backend is not None:
                budget_backend.add(token_delta)
            limits = getattr(self._ctx, "_limits", None)
            if limits is not None:
                limits.add_cost_returning(token_delta)

        # Record circuit breaker success.
        if self._circuit_breaker is not None:
            self._circuit_breaker.record_success()

        # Update stats under async lock.
        async with self._stats_lock:
            stats = self._stats.get(agent_id)
            if stats is not None:
                stats.message_count += 1
                stats.total_cost_usd += actual_cost
                stats._total_latency_ms += latency_ms
                stats._latency_sample_count += 1
                stats.avg_latency_ms = (
                    stats._total_latency_ms / stats._latency_sample_count
                )
                stats.trust_level = identity.trust_level

        # Determine whether the response is a Task or Message object.
        _is_task = hasattr(response, "id") and hasattr(response, "status")
        return A2AResult(
            success=True,
            task=response if _is_task else None,
            message=response if not _is_task else None,
            decision=Decision.ALLOW,
            cost_usd=actual_cost,
            trust_level=identity.trust_level,
        )

    def capabilities(self) -> "AdapterCapabilities":
        """Return the capability descriptor for this adapter."""
        from veronica_core.adapter_capabilities import AdapterCapabilities

        return AdapterCapabilities(
            framework_name="A2A",
            supports_async=True,
            supports_agent_identity=True,
            extra={"protocol_version": "1.0"},
        )

    async def get_stats_async(self) -> dict[str, A2AStats]:
        """Return an immutable snapshot of per-agent usage statistics (async-safe).

        Returns:
            Mapping of agent_id -> A2AStats snapshot.
        """
        async with self._stats_lock:
            return {
                agent_id: dataclasses.replace(stats)
                for agent_id, stats in self._stats.items()
            }

    def get_stats(self) -> dict[str, A2AStats]:
        """Synchronous best-effort snapshot -- safe only from non-async context.

        Callers inside an asyncio event loop should use get_stats_async()
        instead. This method does not acquire the asyncio.Lock, so it is
        safe to call from synchronous code without risk of deadlock.

        Returns a best-effort snapshot: captures dict items at one point in
        time. Individual A2AStats copies may reflect a mix of states under
        concurrent mutation. Acceptable for monitoring and debug callers.

        Returns:
            Mapping of agent_id -> A2AStats snapshot.
        """
        items = list(self._stats.items())
        return {agent_id: dataclasses.replace(stats) for agent_id, stats in items}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_stats(self, agent_id: str) -> None:
        """Create an A2AStats entry for agent_id if it does not exist."""
        if agent_id in self._stats:  # fast path: skip lock for existing agents
            return
        async with self._stats_lock:
            if agent_id not in self._stats:
                cap = self._config.stats_cap
                if len(self._stats) >= cap:
                    logger.warning(
                        "[A2A_CLIENT_ADAPTER] stats tracking %d+ distinct agent IDs; "
                        "dropping new agent ID to prevent DoS",
                        cap,
                    )
                    return
                self._stats[agent_id] = A2AStats(agent_id=agent_id)

    async def _increment_call_count(self, agent_id: str) -> None:
        """Safely increment message_count for agent_id."""
        async with self._stats_lock:
            stats = self._stats.get(agent_id)
            if stats is not None:
                stats.message_count += 1

    async def _increment_error_count(
        self, agent_id: str, cost_usd: float = 0.0
    ) -> None:
        """Safely increment both message_count and error_count for agent_id."""
        async with self._stats_lock:
            stats = self._stats.get(agent_id)
            if stats is not None:
                stats.message_count += 1
                stats.error_count += 1
                stats.total_cost_usd += cost_usd


# ---------------------------------------------------------------------------
# Bound adapter
# ---------------------------------------------------------------------------


class BoundA2AAdapter(A2AClientContainmentAdapter):
    """A2AClientContainmentAdapter with a pre-bound A2A client.

    Exposes send_message() without requiring the caller to pass a client
    on every call. Mirrors _BoundMCPAdapter from mcp_async.py.

    Args:
        client: A2A client satisfying A2AClientProtocol.
        **kwargs: Forwarded to A2AClientContainmentAdapter.__init__().
    """

    def __init__(self, client: A2AClientProtocol, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = client

    async def send_message(  # type: ignore[override]
        self,
        agent_id: str,
        message: Any,
        *,
        tenant_id: str,
        identity: AgentIdentity,
        provenance: A2AIdentityProvenance | None = None,
    ) -> A2AResult:
        """Send a message using the pre-bound client under containment.

        Args:
            agent_id: Remote agent identifier.
            message: A2A message payload.
            tenant_id: Tenant scope for this request.
            identity: Caller's AgentIdentity for governance hooks.
            provenance: Optional verification metadata for the remote agent.

        Returns:
            A2AResult from the parent send_message call.
        """
        return await super().send_message(
            agent_id,
            message,
            tenant_id=tenant_id,
            identity=identity,
            provenance=provenance,
            client=self._client,
        )


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def wrap_a2a_agent(
    client: A2AClientProtocol,
    execution_context: ExecutionContext,
    config: Optional[A2AClientConfig] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    message_costs: Optional[dict[str, A2AMessageCost]] = None,
    failure_predicate: Optional[FailurePredicate] = None,
) -> BoundA2AAdapter:
    """Create a BoundA2AAdapter pre-configured for an A2A client.

    Returns a BoundA2AAdapter with a send_message() convenience method
    that does not require passing the client on each call.

    Args:
        client: A2A client satisfying A2AClientProtocol.
        execution_context: Chain-level containment context.
        config: Optional A2AClientConfig. Defaults to A2AClientConfig().
        circuit_breaker: Optional circuit breaker for the agent connection.
        message_costs: Optional per-agent cost overrides.
        failure_predicate: Optional predicate to filter CB-tripping exceptions.

    Returns:
        BoundA2AAdapter with send_message() bound to the provided client.
    """
    return BoundA2AAdapter(
        client=client,
        execution_context=execution_context,
        config=config,
        circuit_breaker=circuit_breaker,
        message_costs=message_costs,
        failure_predicate=failure_predicate,
    )
