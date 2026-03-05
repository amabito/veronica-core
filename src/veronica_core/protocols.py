"""Protocol definitions for veronica-core framework integration.

These protocols define the contracts that external systems (agent frameworks,
metrics backends, monitoring systems) can implement to integrate with
veronica-core. All protocols use @runtime_checkable so that isinstance()
checks can validate conformance without requiring inheritance.

Usage::

    from veronica_core.protocols import (
        FrameworkAdapterProtocol,
        PlannerProtocol,
        ExecutionGraphObserver,
        ContainmentMetricsProtocol,
    )

    # Check conformance at runtime
    assert isinstance(my_adapter, FrameworkAdapterProtocol)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from veronica_core.adapter_capabilities import AdapterCapabilities


__all__ = [
    "FrameworkAdapterProtocol",
    "ExtendedAdapterProtocol",
    "PlannerProtocol",
    "ExecutionGraphObserver",
    "ContainmentMetricsProtocol",
    "AsyncBudgetBackendProtocol",
    "ReconciliationCallback",
]


@runtime_checkable
class FrameworkAdapterProtocol(Protocol):
    """Minimal contract for integrating a new agent framework with veronica-core.

    All adapters (langchain, ag2, crewai, langgraph, llamaindex, ros2) implement
    this protocol. ``isinstance(adapter, FrameworkAdapterProtocol)`` returns True
    for any adapter that exposes ``capabilities()``.

    For adapters that also support cost/token extraction and containment signalling,
    see :class:`ExtendedAdapterProtocol`.

    Example::

        class MyFrameworkAdapter:
            def capabilities(self) -> AdapterCapabilities:
                return AdapterCapabilities(framework_name="MyFramework")
    """

    def capabilities(self) -> "AdapterCapabilities":
        """Return a static descriptor of this adapter's capabilities.

        Returns:
            AdapterCapabilities instance declaring supported features.
        """
        ...


@runtime_checkable
class ExtendedAdapterProtocol(FrameworkAdapterProtocol, Protocol):
    """Extended contract for adapters that support cost/token extraction and HALT signalling.

    Implement this protocol to teach veronica-core how to extract cost and
    token data from your framework's response objects, and how to signal
    containment decisions (HALT, DEGRADE) back to the framework.

    ``isinstance(adapter, ExtendedAdapterProtocol)`` returns True only for
    adapters that implement all five methods.

    Example::

        class MyFrameworkAdapter:
            def capabilities(self) -> AdapterCapabilities:
                return AdapterCapabilities(framework_name="MyFramework",
                                           supports_cost_extraction=True)

            def extract_cost(self, result: Any) -> float:
                return result.usage.get("cost_usd", 0.0)

            def extract_tokens(self, result: Any) -> tuple[int, int]:
                u = result.usage
                return u["input_tokens"], u["output_tokens"]

            def handle_halt(self, reason: str) -> Any:
                return {"error": "halt", "reason": reason}

            def handle_degrade(self, reason: str, suggestion: str) -> Any:
                return {"warning": "degrade", "reason": reason, "suggestion": suggestion}
    """

    def extract_cost(self, result: Any) -> float:
        """Return the USD cost for a completed LLM call.

        Args:
            result: The raw response object from the framework.

        Returns:
            Cost in USD. Return 0.0 if not available.
        """
        ...

    def extract_tokens(self, result: Any) -> tuple[int, int]:
        """Return the (input_tokens, output_tokens) for a completed LLM call.

        Args:
            result: The raw response object from the framework.

        Returns:
            Tuple of (input_token_count, output_token_count). Return (0, 0)
            if not available.
        """
        ...

    def handle_halt(self, reason: str) -> Any:
        """Translate a HALT decision into a framework-native response.

        Called when veronica-core's containment logic decides to stop a call
        (cost ceiling exceeded, circuit open, etc.). The return value replaces
        the normal LLM response in the agent's message stream.

        Args:
            reason: Human-readable explanation of why the call was halted.

        Returns:
            Framework-native object to return to the caller (e.g., an error
            message dict, a sentinel value, or None).
        """
        ...

    def handle_degrade(self, reason: str, suggestion: str) -> Any:
        """Translate a DEGRADE decision into a framework-native response.

        Called when veronica-core recommends model downgrade (e.g., switch
        from gpt-4 to gpt-3.5 due to budget pressure). The framework may
        choose to act on *suggestion* or ignore it.

        Args:
            reason: Why degradation is recommended.
            suggestion: The recommended degraded model or approach.

        Returns:
            Framework-native object to forward to the caller.
        """
        ...


@runtime_checkable
class PlannerProtocol(Protocol):
    """Stateless policy proposer for adaptive containment.

    The Planner proposes policies based on chain metadata and prior safety
    events. The veronica-core kernel enforces those policies. Planners do
    NOT enforce anything themselves — they only propose.

    This separation allows policy logic to be tested independently of
    enforcement and swapped at runtime without changing the enforcement layer.

    Example::

        class ConservativePlanner:
            def propose_policy(self, chain_metadata: Any, prior_events: list) -> dict:
                # Tighten limits if any prior violations were observed
                if any(e.get("severity") == "critical" for e in prior_events):
                    return {"max_cost_usd": 0.10, "max_steps": 5}
                return {"max_cost_usd": 1.0, "max_steps": 50}

            def on_safety_event(self, event: Any) -> None:
                # Log or alert; do NOT mutate enforcement state
                pass
    """

    def propose_policy(self, chain_metadata: Any, prior_events: list) -> dict:
        """Propose a policy configuration for the given chain.

        Args:
            chain_metadata: ChainMetadata (or equivalent) describing the
                current request chain (org_id, team, service, tags, etc.).
            prior_events: List of SafetyEvent dicts from earlier in the
                same chain or session. May be empty for the first call.

        Returns:
            Dict of policy parameters to apply. Keys should match fields of
            ExecutionConfig or ShieldConfig. Unknown keys are ignored.
        """
        ...

    def on_safety_event(self, event: Any) -> None:
        """Notification hook called when a SafetyEvent is emitted.

        Called synchronously by the containment kernel. Must not raise.
        Must not mutate shared state that could affect enforcement.

        Args:
            event: SafetyEvent or equivalent dict describing the incident.
        """
        ...


@runtime_checkable
class ExecutionGraphObserver(Protocol):
    """Observer for execution lifecycle events in an ExecutionGraph.

    Attach observers to ExecutionGraph to receive callbacks as nodes start,
    complete, or fail. Use this to stream execution telemetry to external
    systems (OpenTelemetry, Datadog, structured logging, etc.) without
    coupling the graph to those systems.

    All methods must be non-blocking. Do not perform I/O inline; queue
    events for async delivery if needed.

    Example::

        class StructuredLogObserver:
            def on_node_start(self, node_id, operation, metadata):
                logger.info("node.start", extra={"node_id": node_id, ...})

            def on_node_complete(self, node_id, cost_usd, duration_ms):
                logger.info("node.complete", extra={"node_id": node_id, ...})

            def on_node_failed(self, node_id, error):
                logger.error("node.failed", extra={"node_id": node_id, ...})

            def on_decision(self, node_id, decision, reason):
                logger.info("node.decision", extra={"decision": decision, ...})
    """

    def on_node_start(self, node_id: str, operation: str, metadata: dict) -> None:
        """Called when a node transitions to running status.

        Args:
            node_id: Unique node identifier within the chain (e.g., "n000001").
            operation: Human-readable operation name (e.g., "plan_step").
            metadata: Arbitrary key-value pairs from the node (may be empty).
        """
        ...

    def on_node_complete(
        self, node_id: str, cost_usd: float, duration_ms: float
    ) -> None:
        """Called when a node transitions to success status.

        Args:
            node_id: Unique node identifier.
            cost_usd: Actual USD cost for this node.
            duration_ms: Wall-clock duration from start to completion.
        """
        ...

    def on_node_failed(self, node_id: str, error: str) -> None:
        """Called when a node transitions to fail or halt status.

        Args:
            node_id: Unique node identifier.
            error: Error class name or stop reason describing the failure.
        """
        ...

    def on_decision(self, node_id: str, decision: str, reason: str) -> None:
        """Called when veronica-core makes a containment decision for a node.

        Args:
            node_id: The node the decision applies to.
            decision: "ALLOW", "HALT", "DEGRADE", or "COOLDOWN".
            reason: Human-readable explanation of the decision.
        """
        ...


@runtime_checkable
class ContainmentMetricsProtocol(Protocol):
    """Standard metrics interface for veronica-core containment telemetry.

    Implement this protocol to forward containment metrics to your metrics
    backend (Prometheus, Datadog, StatsD, OpenTelemetry, etc.).

    All methods must be non-blocking. Never raise; silently drop on error.

    Example::

        class PrometheusMetrics:
            def record_cost(self, agent_id, cost_usd):
                cost_counter.labels(agent=agent_id).inc(cost_usd)

            def record_tokens(self, agent_id, input_tokens, output_tokens):
                token_counter.labels(agent=agent_id, direction="in").inc(input_tokens)
                token_counter.labels(agent=agent_id, direction="out").inc(output_tokens)

            def record_decision(self, agent_id, decision):
                decision_counter.labels(agent=agent_id, decision=decision).inc()

            def record_circuit_state(self, entity_id, state):
                circuit_gauge.labels(entity=entity_id).set({"CLOSED": 0, "OPEN": 1}.get(state, 2))

            def record_latency(self, agent_id, duration_ms):
                latency_histogram.labels(agent=agent_id).observe(duration_ms / 1000)
    """

    def record_cost(self, agent_id: str, cost_usd: float) -> None:
        """Record USD cost for one LLM call.

        Args:
            agent_id: Identifier for the agent or chain that incurred the cost.
            cost_usd: Cost in USD for this call.
        """
        ...

    def record_tokens(
        self, agent_id: str, input_tokens: int, output_tokens: int
    ) -> None:
        """Record token counts for one LLM call.

        Args:
            agent_id: Identifier for the agent or chain.
            input_tokens: Number of prompt/input tokens consumed.
            output_tokens: Number of completion/output tokens generated.
        """
        ...

    def record_decision(self, agent_id: str, decision: str) -> None:
        """Record a containment decision (ALLOW, HALT, DEGRADE, COOLDOWN).

        Args:
            agent_id: Identifier for the agent or chain receiving the decision.
            decision: Decision label string.
        """
        ...

    def record_circuit_state(self, entity_id: str, state: str) -> None:
        """Record the current circuit breaker state for a protected entity.

        Args:
            entity_id: Identifier for the circuit (e.g., circuit_id or agent name).
            state: "CLOSED", "OPEN", or "HALF_OPEN".
        """
        ...

    def record_latency(self, agent_id: str, duration_ms: float) -> None:
        """Record wall-clock latency for one LLM or tool call.

        Args:
            agent_id: Identifier for the agent or chain.
            duration_ms: Duration in milliseconds from dispatch to completion.
        """
        ...


@runtime_checkable
class AsyncBudgetBackendProtocol(Protocol):
    """Protocol for async-capable budget backends.

    Async budget backends must support reserve/commit/rollback for two-phase
    accounting, plus a simple get() for current committed cost.
    """

    async def reserve(self, amount: float, ceiling: float) -> str:
        """Atomically reserve amount against ceiling. Returns reservation ID."""
        ...

    async def commit(self, reservation_id: str) -> float:
        """Commit a reservation. Returns new total committed cost."""
        ...

    async def rollback(self, reservation_id: str) -> None:
        """Roll back a reservation without charging cost."""
        ...

    async def get(self) -> float:
        """Return the current committed cost."""
        ...


@runtime_checkable
class ReconciliationCallback(Protocol):
    """Callback invoked after a successful wrap call to reconcile estimated vs actual cost.

    Implement this protocol to receive notifications when the actual cost of a
    call differs from the estimated cost. Useful for cost tracking, alerting,
    and budget adjustment.
    """

    def on_reconcile(self, estimated_cost: float, actual_cost: float) -> None:
        """Called after a successful wrap_llm_call or wrap_tool_call.

        Args:
            estimated_cost: The cost_estimate_hint from WrapOptions.
            actual_cost: The actual cost computed after the call completed.
        """
        ...
