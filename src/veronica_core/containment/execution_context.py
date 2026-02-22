"""ExecutionContext — chain-level containment for VERONICA agent runs.

Provides a lifespan-scoped container that enforces chain-wide limits
(cost ceiling, step limit, retry budget, timeout) across all LLM and
tool calls within a single agent run or request chain.

Wraps ShieldPipeline without replacing it. Existing per-call pipeline
usage continues to work unchanged.

Usage::

    from veronica_core.containment import ExecutionContext, ExecutionConfig

    config = ExecutionConfig(
        max_cost_usd=1.0,
        max_steps=50,
        max_retries_total=10,
        timeout_ms=30_000,
    )
    with ExecutionContext(config=config, pipeline=my_pipeline) as ctx:
        decision = ctx.wrap_llm_call(
            fn=lambda: client.chat(...),
            options=WrapOptions(operation_name="plan_step", cost_estimate_hint=0.01),
        )
        if decision == Decision.HALT:
            break
"""

# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------
# STEP-C (wiring commit):
#   - CircuitBreaker.check() wired before fn() dispatch (breaker_check stage)
#   - pipeline.before_charge() wired after cost computed, before accumulation
#   - kind="tool" routes to pipeline.before_tool_call(); before_charge skipped
# ---------------------------------------------------------------------------

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

from veronica_core.containment.execution_graph import ExecutionGraph
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision, ToolCallContext

# ShieldPipeline imported at runtime to avoid circular imports at module load.
# TYPE_CHECKING block used for type hints only.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from veronica_core.circuit_breaker import CircuitBreaker
    from veronica_core.shield.pipeline import ShieldPipeline


__all__ = [
    "CancellationToken",
    "ChainMetadata",
    "ContextSnapshot",
    "ExecutionConfig",
    "ExecutionContext",
    "NodeRecord",
    "WrapOptions",
]


# ---------------------------------------------------------------------------
# CancellationToken
# ---------------------------------------------------------------------------


class CancellationToken:
    """Cooperative cancellation signal backed by threading.Event.

    Wrap long-running operations with ``is_cancelled`` checks or
    call ``cancel()`` to signal shutdown to all cooperating threads.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Signal cancellation. Idempotent."""
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        """True once cancel() has been called."""
        return self._event.is_set()

    def wait(self, timeout_s: float | None = None) -> bool:
        """Block until cancelled or timeout expires.

        Returns True if cancelled, False if timeout elapsed first.
        """
        return self._event.wait(timeout=timeout_s)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainMetadata:
    """Immutable descriptor for one request chain.

    All fields except ``request_id`` and ``chain_id`` are optional so
    callers can populate only what they have available.
    """

    request_id: str
    chain_id: str
    org_id: str = ""
    team: str = ""
    service: str = ""
    user_id: str | None = None
    model: str | None = None
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionConfig:
    """Hard limits for one chain execution.

    All numeric limits must be positive.

    Attributes:
        max_cost_usd: Chain-level USD spending ceiling. Once
            ``cost_usd_accumulated`` reaches this value, all subsequent
            wrap calls return Decision.HALT without executing the callable.
        max_steps: Maximum number of successful wrap calls. Prevents
            runaway agent loops.
        max_retries_total: Chain-wide retry budget. Counts retries across
            all nodes. Once exhausted, wrap calls return Decision.HALT.
        timeout_ms: Wall-clock timeout in milliseconds. 0 disables the
            timeout. When elapsed, the CancellationToken is signalled and
            all new wrap calls return Decision.HALT immediately.
    """

    max_cost_usd: float
    max_steps: int
    max_retries_total: int
    timeout_ms: int = 0


@dataclass(frozen=True)
class WrapOptions:
    """Per-call options for wrap_llm_call / wrap_tool_call.

    All fields are optional. Omitting a field inherits the chain-level
    default from ExecutionConfig.
    """

    operation_name: str = ""
    cost_estimate_hint: float = 0.0
    timeout_ms: int | None = None
    retry_policy_override: int | None = None
    model: str | None = None
    response_hint: Any = None


@dataclass
class NodeRecord:
    """Record of a single LLM or tool call within the chain.

    Created at the start of each wrap call and updated when the call
    completes. Captured in ContextSnapshot.nodes.
    """

    node_id: str
    parent_id: str | None
    kind: Literal["llm", "tool"]
    operation_name: str
    start_ts: datetime
    end_ts: datetime | None
    status: Literal["ok", "halted", "aborted", "timeout", "error"]
    cost_usd: float
    retries_used: int


@dataclass(frozen=True)
class ContextSnapshot:
    """Immutable view of chain state at a point in time.

    Returned by ExecutionContext.get_snapshot(). Safe to store and
    compare across calls; all mutable state is copied on creation.
    """

    chain_id: str
    request_id: str
    step_count: int
    cost_usd_accumulated: float
    retries_used: int
    aborted: bool
    abort_reason: str | None
    elapsed_ms: float
    nodes: list[NodeRecord]
    events: list[SafetyEvent]
    graph_summary: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# ExecutionContext
# ---------------------------------------------------------------------------

_STOP_REASON_EVENT_TYPE: dict[str, str] = {
    "aborted": "CHAIN_ABORTED",
    "budget_exceeded": "CHAIN_BUDGET_EXCEEDED",
    "step_limit_exceeded": "CHAIN_STEP_LIMIT_EXCEEDED",
    "retry_budget_exceeded": "CHAIN_RETRY_BUDGET_EXCEEDED",
    "timeout": "CHAIN_TIMEOUT",
    "circuit_open": "CHAIN_CIRCUIT_OPEN",
}


class ExecutionContext:
    """Chain-level containment for one agent run or request.

    Enforces hard limits (cost ceiling, step limit, retry budget, timeout)
    across all nested LLM and tool calls. Wraps ShieldPipeline for per-call
    hook evaluation; does not replace it.

    Can be used as a context manager or standalone::

        # Context manager (auto-cleanup on exit)
        with ExecutionContext(config=cfg, pipeline=pl, metadata=meta) as ctx:
            ctx.wrap_llm_call(fn=my_fn)

        # Standalone
        ctx = ExecutionContext(config=cfg)
        ctx.wrap_llm_call(fn=my_fn)
        ctx.abort("user cancelled")
        snap = ctx.get_snapshot()
    """

    def __init__(
        self,
        config: ExecutionConfig,
        pipeline: ShieldPipeline | None = None,
        metadata: ChainMetadata | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._config = config
        self._pipeline = pipeline
        self._circuit_breaker = circuit_breaker
        self._metadata = metadata or ChainMetadata(
            request_id=str(uuid.uuid4()),
            chain_id=str(uuid.uuid4()),
        )

        # Mutable chain-level counters (protected by _lock)
        self._lock = threading.Lock()
        self._step_count: int = 0
        self._cost_usd_accumulated: float = 0.0
        self._retries_used: int = 0
        self._aborted: bool = False
        self._abort_reason: str | None = None
        self._nodes: list[NodeRecord] = []
        self._events: list[SafetyEvent] = []

        # Execution graph for DAG tracking of all nodes.
        self._graph = ExecutionGraph(chain_id=self._metadata.chain_id)
        self._root_node_id = self._graph.create_root("chain_root", {})
        # threading.local() stack for nested parent tracking.
        self._node_stack = threading.local()

        # Timeout bookkeeping
        self._start_time: float = time.monotonic()
        self._cancellation_token = CancellationToken()
        self._timeout_thread: threading.Thread | None = None

        if config.timeout_ms > 0:
            self._start_timeout_watcher()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> ExecutionContext:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        # Signal the timeout watcher thread to stop and wait for it to exit.
        # cancel() unblocks the thread's wait() call; join() ensures the thread
        # has actually finished before the context exits, preventing thread leaks
        # in test suites and short-lived programs.
        self._cancellation_token.cancel()
        if self._timeout_thread is not None and self._timeout_thread.is_alive():
            self._timeout_thread.join(timeout=1.0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def wrap_llm_call(
        self,
        fn: Callable[[], Any],
        options: WrapOptions | None = None,
    ) -> Decision:
        """Execute *fn* under chain-level containment.

        Checks all hard limits before calling *fn*. If any limit is
        exceeded the callable is never invoked.

        Args:
            fn: Zero-argument callable representing the LLM call.
            options: Optional per-call configuration.

        Returns:
            Decision.ALLOW on clean completion.
            Decision.HALT when any chain-level limit is exceeded.
            Decision.RETRY when the call failed but retries remain.
            Any other Decision forwarded from the ShieldPipeline.
        """
        return self._wrap(fn, kind="llm", options=options)

    def wrap_tool_call(
        self,
        fn: Callable[[], Any],
        options: WrapOptions | None = None,
    ) -> Decision:
        """Execute *fn* under chain-level containment (tool variant).

        Identical to wrap_llm_call but records kind="tool" in the NodeRecord
        and routes through tool-call hooks when the pipeline supports them.

        Args:
            fn: Zero-argument callable representing the tool call.
            options: Optional per-call configuration.

        Returns:
            Decision.ALLOW on clean completion.
            Decision.HALT when any chain-level limit is exceeded.
        """
        return self._wrap(fn, kind="tool", options=options)

    def record_event(self, event: SafetyEvent) -> None:
        """Append *event* to the chain-level event log.

        Use this when application code emits SafetyEvent instances outside
        of wrap_llm_call / wrap_tool_call.

        Args:
            event: SafetyEvent to record.
        """
        with self._lock:
            self._events.append(event)

    def get_snapshot(self) -> ContextSnapshot:
        """Return an immutable snapshot of current chain state.

        Safe to call at any time, including from finalisation code
        after abort().

        Returns:
            ContextSnapshot with copies of all mutable state. The
            graph_summary field contains aggregate counters from the
            ExecutionGraph (total_cost_usd, total_llm_calls,
            total_tool_calls, total_retries, max_depth).
        """
        with self._lock:
            elapsed_ms = (time.monotonic() - self._start_time) * 1000.0
            graph_snap = self._graph.snapshot()
            graph_summary = graph_snap["aggregates"]
            return ContextSnapshot(
                chain_id=self._metadata.chain_id,
                request_id=self._metadata.request_id,
                step_count=self._step_count,
                cost_usd_accumulated=self._cost_usd_accumulated,
                retries_used=self._retries_used,
                aborted=self._aborted,
                abort_reason=self._abort_reason,
                elapsed_ms=elapsed_ms,
                nodes=list(self._nodes),
                events=list(self._events),
                graph_summary=graph_summary,
            )

    def get_graph_snapshot(self) -> dict[str, Any]:
        """Return the full ExecutionGraph snapshot as a JSON-serializable dict.

        Contains all nodes, aggregates, and chain metadata.

        Returns:
            dict as produced by ExecutionGraph.snapshot().
        """
        return self._graph.snapshot()

    def abort(self, reason: str) -> None:
        """Cancel all pending work and prevent future wrap calls.

        Idempotent. Subsequent calls to wrap_llm_call / wrap_tool_call
        return Decision.HALT immediately without executing the callable.

        Does not raise an exception.

        Args:
            reason: Human-readable explanation recorded in the snapshot.
        """
        with self._lock:
            if not self._aborted:
                self._aborted = True
                self._abort_reason = reason
                self._cancellation_token.cancel()
                self._emit_chain_event("aborted", reason)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _wrap(
        self,
        fn: Callable[[], Any],
        kind: Literal["llm", "tool"],
        options: WrapOptions | None,
    ) -> Decision:
        """Common implementation for wrap_llm_call and wrap_tool_call."""
        opts = options or WrapOptions()
        node_id = str(uuid.uuid4())

        # Determine parent node (last completed node if any).
        with self._lock:
            parent_id = self._nodes[-1].node_id if self._nodes else None

        node = NodeRecord(
            node_id=node_id,
            parent_id=parent_id,
            kind=kind,
            operation_name=opts.operation_name,
            start_ts=datetime.now(timezone.utc),
            end_ts=None,
            status="ok",
            cost_usd=0.0,
            retries_used=0,
        )

        # Determine graph parent using threading.local() stack.
        stack: list[str] = getattr(self._node_stack, "stack", None)
        if stack is None:
            self._node_stack.stack = []
            stack = self._node_stack.stack
        graph_parent_id = stack[-1] if stack else self._root_node_id

        # Create graph node and push onto stack.
        graph_node_id = self._graph.begin_node(
            parent_id=graph_parent_id,
            kind=kind,
            name=opts.operation_name or "unnamed",
            model=self._metadata.model if kind == "llm" else None,
        )
        stack.append(graph_node_id)

        # Pre-flight: chain-level limit check.
        halt_reason = self._check_limits()
        if halt_reason is not None:
            node.status = "halted"
            node.end_ts = datetime.now(timezone.utc)
            with self._lock:
                self._nodes.append(node)
            stack.pop()
            self._graph.mark_halt(graph_node_id, stop_reason=halt_reason)
            return Decision.HALT

        # Pre-flight: cost estimate check (before calling fn).
        # Hold a single lock for the entire read-check-emit-append sequence to
        # prevent another thread from modifying _cost_usd_accumulated between
        # the read and the limit comparison.
        _budget_exceeded = False
        if opts.cost_estimate_hint > 0.0:
            with self._lock:
                projected = self._cost_usd_accumulated + opts.cost_estimate_hint
                if projected > self._config.max_cost_usd:
                    _budget_exceeded = True
                    self._emit_chain_event(
                        "budget_exceeded",
                        f"cost estimate ${opts.cost_estimate_hint:.4f} would exceed "
                        f"chain ceiling ${self._config.max_cost_usd:.4f}",
                    )
                    node.status = "halted"
                    node.end_ts = datetime.now(timezone.utc)
                    self._nodes.append(node)
            if _budget_exceeded:
                stack.pop()
                self._graph.mark_halt(graph_node_id, stop_reason="budget_exceeded")
                return Decision.HALT

        # Pipeline pre-dispatch check.
        if self._pipeline is not None:
            tool_ctx = self._make_tool_ctx(node_id, opts)
            if kind == "llm":
                pipeline_decision = self._pipeline.before_llm_call(tool_ctx)
            else:
                pipeline_decision = self._pipeline.before_tool_call(tool_ctx)
            if pipeline_decision != Decision.ALLOW:
                # Mirror pipeline events into chain-level log.
                with self._lock:
                    for ev in self._pipeline.get_events():
                        if ev not in self._events:
                            self._events.append(ev)
                node.status = "halted"
                node.end_ts = datetime.now(timezone.utc)
                with self._lock:
                    self._nodes.append(node)
                stack.pop()
                self._graph.mark_halt(graph_node_id, stop_reason="pipeline_halt")
                return pipeline_decision

        # CircuitBreaker pre-dispatch check.
        if self._circuit_breaker is not None:
            from veronica_core.runtime_policy import PolicyContext
            policy_ctx = PolicyContext(
                cost_usd=self._cost_usd_accumulated,
                step_count=self._step_count,
                chain_id=self._metadata.chain_id,
                entity_id=self._metadata.user_id or "",
            )
            pd = self._circuit_breaker.check(policy_ctx)
            if not pd.allowed:
                with self._lock:
                    self._emit_chain_event(
                        "circuit_open",
                        f"circuit breaker denied: {pd.reason}",
                    )
                node.status = "halted"
                node.end_ts = datetime.now(timezone.utc)
                with self._lock:
                    self._nodes.append(node)
                stack.pop()
                self._graph.mark_halt(graph_node_id, stop_reason="circuit_open")
                return Decision.HALT

        # Dispatch the callable.
        self._graph.mark_running(graph_node_id)

        # Drain any divergence events produced by mark_running and forward
        # them to the chain-level event log.  Divergence does NOT halt
        # execution; the Decision remains ALLOW unless another condition fires.
        for div_evt in self._graph.drain_divergence_events():
            safe_evt = SafetyEvent(
                event_type="divergence_suspected",
                decision=Decision.ALLOW,
                reason=(
                    f"repeated {div_evt['signature'][0]}/{div_evt['signature'][1]}"
                    f" x{div_evt['repeat_count']}"
                ),
                hook="ExecutionGraph",
                request_id=self._metadata.request_id,
                metadata={
                    "signature": div_evt["signature"],
                    "repeat_count": div_evt["repeat_count"],
                    "chain_id": div_evt["chain_id"],
                    "last_node_id": graph_node_id,
                },
            )
            with self._lock:
                if safe_evt not in self._events:
                    self._events.append(safe_evt)

        call_start = time.monotonic()
        try:
            fn()
        except BaseException as exc:
            call_elapsed_ms = (time.monotonic() - call_start) * 1000.0

            # Check for timeout-driven cancellation.
            if self._cancellation_token.is_cancelled:
                node.status = "timeout"
                node.end_ts = datetime.now(timezone.utc)
                with self._lock:
                    self._nodes.append(node)
                stack.pop()
                self._graph.mark_halt(graph_node_id, stop_reason="timeout")
                return Decision.HALT

            # Pipeline error hook.
            if self._pipeline is not None:
                tool_ctx = self._make_tool_ctx(node_id, opts)
                error_decision = self._pipeline.on_error(tool_ctx, exc)
            else:
                error_decision = Decision.RETRY

            if self._circuit_breaker is not None:
                self._circuit_breaker.record_failure()

            with self._lock:
                self._retries_used += 1
                node.retries_used += 1

            node.status = "error"
            node.end_ts = datetime.now(timezone.utc)
            with self._lock:
                self._nodes.append(node)
            stack.pop()
            self._graph.mark_failure(graph_node_id, error_class=type(exc).__name__)

            if error_decision == Decision.HALT:
                return Decision.HALT
            return Decision.RETRY

        # Success path.
        call_elapsed_ms = (time.monotonic() - call_start) * 1000.0  # noqa: F841

        actual_cost = opts.cost_estimate_hint
        if actual_cost == 0.0 and kind == "llm":
            from veronica_core.pricing import estimate_cost_usd, extract_usage_from_response
            model_name = opts.model or ""
            if not model_name and hasattr(self, "_metadata") and self._metadata:
                model_name = self._metadata.model or ""
            usage = None
            if opts.response_hint is not None:
                usage = extract_usage_from_response(opts.response_hint)
            if usage is not None:
                actual_cost = estimate_cost_usd(model_name, usage[0], usage[1])
            elif model_name:
                _ev = SafetyEvent(
                    event_type="COST_ESTIMATION_SKIPPED",
                    decision=Decision.ALLOW,
                    reason=f"model={model_name!r} known but response_hint not provided; cost_usd=0.0 recorded",
                    hook="AutoPricing",
                )
                with self._lock:
                    self._events.append(_ev)

        # Pipeline budget check (post-call, LLM calls only).
        if kind == "llm" and self._pipeline is not None and actual_cost > 0.0:
            tool_ctx = self._make_tool_ctx(node_id, opts, cost_usd=actual_cost)
            charge_decision = self._pipeline.before_charge(tool_ctx, actual_cost)
            if charge_decision != Decision.ALLOW:
                with self._lock:
                    for ev in self._pipeline.get_events():
                        if ev not in self._events:
                            self._events.append(ev)
                node.status = "halted"
                node.end_ts = datetime.now(timezone.utc)
                with self._lock:
                    self._nodes.append(node)
                stack.pop()
                self._graph.mark_halt(graph_node_id, stop_reason="before_charge_halt")
                return charge_decision

        if self._circuit_breaker is not None:
            self._circuit_breaker.record_success()

        with self._lock:
            self._step_count += 1
            self._cost_usd_accumulated += actual_cost
            node.cost_usd = actual_cost
            node.status = "ok"
            node.end_ts = datetime.now(timezone.utc)
            self._nodes.append(node)

            # Mirror any new pipeline events.
            if self._pipeline is not None:
                for ev in self._pipeline.get_events():
                    if ev not in self._events:
                        self._events.append(ev)

        stack.pop()
        self._graph.mark_success(graph_node_id, cost_usd=actual_cost)
        return Decision.ALLOW

    def _check_limits(self) -> str | None:
        """Return a stop-reason string if any chain-level limit is exceeded.

        Returns None when all limits are within bounds.

        Checked in priority order:
        1. aborted flag
        2. cost ceiling
        3. step limit
        4. retry budget
        5. timeout / cancellation
        """
        with self._lock:
            if self._aborted:
                return "aborted"

            if self._cost_usd_accumulated >= self._config.max_cost_usd:
                reason = (
                    f"cost ${self._cost_usd_accumulated:.4f} >= "
                    f"ceiling ${self._config.max_cost_usd:.4f}"
                )
                self._emit_chain_event("budget_exceeded", reason)
                return "budget_exceeded"

            if self._step_count >= self._config.max_steps:
                reason = f"steps {self._step_count} >= limit {self._config.max_steps}"
                self._emit_chain_event("step_limit_exceeded", reason)
                return "step_limit_exceeded"

            if self._retries_used >= self._config.max_retries_total:
                reason = (
                    f"retries {self._retries_used} >= "
                    f"budget {self._config.max_retries_total}"
                )
                self._emit_chain_event("retry_budget_exceeded", reason)
                return "retry_budget_exceeded"

        if self._cancellation_token.is_cancelled:
            with self._lock:
                self._emit_chain_event("timeout", "cancellation token signalled")
            return "timeout"

        return None

    def _make_tool_ctx(
        self,
        node_id: str,
        opts: WrapOptions,
        cost_usd: float | None = None,
    ) -> ToolCallContext:
        """Construct a ToolCallContext from chain metadata and per-call options.

        Args:
            node_id: Unique identifier for this node (used as session_id).
            opts: Per-call options.
            cost_usd: Actual cost to populate, if known.

        Returns:
            ToolCallContext populated from ChainMetadata and WrapOptions.
        """
        return ToolCallContext(
            request_id=self._metadata.request_id,
            user_id=self._metadata.user_id,
            session_id=node_id,
            tool_name=opts.operation_name or None,
            model=self._metadata.model,
            cost_usd=cost_usd,
            metadata={
                "chain_id": self._metadata.chain_id,
                "org_id": self._metadata.org_id,
                "team": self._metadata.team,
                "service": self._metadata.service,
                **self._metadata.tags,
            },
        )

    def _emit_chain_event(self, stop_reason: str, detail: str) -> None:
        """Append a chain-level SafetyEvent for *stop_reason*.

        Must be called with self._lock already held or from a context where
        thread safety is guaranteed by the caller.

        Args:
            stop_reason: Key from _STOP_REASON_EVENT_TYPE.
            detail: Human-readable explanation.
        """
        event_type = _STOP_REASON_EVENT_TYPE.get(stop_reason, stop_reason.upper())
        event = SafetyEvent(
            event_type=event_type,
            decision=Decision.HALT,
            reason=detail,
            hook="ExecutionContext",
            request_id=self._metadata.request_id,
        )
        # Append only if not already present (idempotent for repeated limit checks).
        if event not in self._events:
            self._events.append(event)

    def _start_timeout_watcher(self) -> None:
        """Start a daemon thread that signals cancellation after timeout_ms."""

        timeout_s = self._config.timeout_ms / 1000.0

        def watcher() -> None:
            cancelled = self._cancellation_token.wait(timeout_s=timeout_s)
            if not cancelled:
                # Timeout elapsed; signal cancellation.
                with self._lock:
                    self._emit_chain_event(
                        "timeout",
                        f"timeout_ms={self._config.timeout_ms} elapsed",
                    )
                self._cancellation_token.cancel()

        self._timeout_thread = threading.Thread(
            target=watcher,
            daemon=True,
            name=f"veronica-timeout-{self._metadata.chain_id[:8]}",
        )
        self._timeout_thread.start()
