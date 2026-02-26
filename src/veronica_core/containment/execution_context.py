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
# v0.11 — WrapOptions.partial_buffer field; _current_partial_buffer ContextVar;
#          get_current_partial_buffer(); ExecutionContext.get_partial_result().
# ---------------------------------------------------------------------------

from __future__ import annotations

import contextvars
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
    from veronica_core.partial import PartialResultBuffer

# ContextVar that holds the active PartialResultBuffer for the current wrap call.
# Reset to None after each wrap call completes (or raises).
_current_partial_buffer: contextvars.ContextVar[PartialResultBuffer | None] = (
    contextvars.ContextVar("veronica_partial_buffer", default=None)
)


def get_current_partial_buffer() -> PartialResultBuffer | None:
    """Return the PartialResultBuffer set for the current wrap call, or None."""
    return _current_partial_buffer.get()


def attach_partial_buffer(buf: PartialResultBuffer) -> None:
    """Set *buf* as the partial buffer for the current wrap_llm_call context.

    Raises RuntimeError if called outside an active wrap_llm_call (i.e., when
    no ContextVar token is set).

    Raises RuntimeError if a different buffer is already attached to the current
    context (prevents silent overwrite of an in-progress partial capture).
    """
    current = _current_partial_buffer.get()
    if current is None:
        raise RuntimeError(
            "attach_partial_buffer() called outside an active wrap_llm_call context."
        )
    if current is not buf:
        raise RuntimeError(
            "attach_partial_buffer() called with a different buffer than the one "
            "already attached to the current wrap_llm_call context."
        )
    _current_partial_buffer.set(buf)


__all__ = [
    "CancellationToken",
    "ChainMetadata",
    "ContextSnapshot",
    "ExecutionConfig",
    "ExecutionContext",
    "NodeRecord",
    "WrapOptions",
    "get_current_partial_buffer",
    "attach_partial_buffer",
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
    budget_backend: "Any | None" = None  # BudgetBackend instance for cross-process tracking
    redis_url: str | None = None  # Convenience: auto-create RedisBudgetBackend


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
    partial_buffer: "PartialResultBuffer | None" = None


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
    partial_buffer: "PartialResultBuffer | None" = None


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
    parent_chain_id: str | None = None


# ---------------------------------------------------------------------------
# ExecutionContext
# ---------------------------------------------------------------------------

_STOP_REASON_EVENT_TYPE: dict[str, str] = {
    "aborted": "CHAIN_ABORTED",
    "budget_exceeded": "CHAIN_BUDGET_EXCEEDED",
    "budget_exceeded_by_child": "CHAIN_BUDGET_EXCEEDED_BY_CHILD",
    "step_limit_exceeded": "CHAIN_STEP_LIMIT_EXCEEDED",
    "retry_budget_exceeded": "CHAIN_RETRY_BUDGET_EXCEEDED",
    "timeout": "CHAIN_TIMEOUT",
    "circuit_open": "CHAIN_CIRCUIT_OPEN",
}

# Maximum number of SafetyEvents stored per chain to prevent memory exhaustion
# from flooding callers or repeated limit-check emissions.
_MAX_CHAIN_EVENTS: int = 1_000


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
        parent: "ExecutionContext | None" = None,
    ) -> None:
        self._config = config
        self._pipeline = pipeline
        self._circuit_breaker = circuit_breaker
        self._metadata = metadata or ChainMetadata(
            request_id=str(uuid.uuid4()),
            chain_id=str(uuid.uuid4()),
        )
        self._parent: ExecutionContext | None = parent

        if self._circuit_breaker is not None:
            self._circuit_breaker.bind_to_context(self._metadata.chain_id)

        # Budget backend setup (v0.10.0)
        if config.budget_backend is not None:
            self._budget_backend = config.budget_backend
        elif config.redis_url:
            from veronica_core.distributed import get_default_backend
            self._budget_backend = get_default_backend(
                redis_url=config.redis_url,
                chain_id=self._metadata.chain_id,
            )
        else:
            from veronica_core.distributed import LocalBudgetBackend
            self._budget_backend = LocalBudgetBackend()

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

        # Partial buffers keyed by graph_node_id. Populated when WrapOptions.partial_buffer
        # is set; used by get_partial_result() to look up partial text per node.
        self._partial_buffers: dict[str, PartialResultBuffer] = {}

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
        if hasattr(self, "_budget_backend"):
            self._budget_backend.close()

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

        Silently drops events once the internal cap (_MAX_CHAIN_EVENTS) is
        reached to prevent memory exhaustion from flooding callers.

        Args:
            event: SafetyEvent to record.
        """
        with self._lock:
            if len(self._events) < _MAX_CHAIN_EVENTS:
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
                parent_chain_id=self._parent._metadata.chain_id if self._parent is not None else None,
            )

    def get_graph_snapshot(self) -> dict[str, Any]:
        """Return the full ExecutionGraph snapshot as a JSON-serializable dict.

        Contains all nodes, aggregates, and chain metadata.

        Returns:
            dict as produced by ExecutionGraph.snapshot().
        """
        return self._graph.snapshot()

    def get_partial_result(self, node_id: str) -> "PartialResultBuffer | None":
        """Return the PartialResultBuffer for *node_id*, or None if none was attached.

        Args:
            node_id: The graph_node_id associated with the wrap call.
        """
        return self._partial_buffers.get(node_id)

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
            partial_buffer=opts.partial_buffer,
        )

        stack, graph_node_id = self._begin_graph_node(kind, opts)

        # Pre-flight: chain-level limit check.
        halt_reason = self._check_limits()
        if halt_reason is not None:
            return self._halt_node(node, stack, graph_node_id, halt_reason)

        # Pre-flight: cost estimate check (before calling fn).
        if opts.cost_estimate_hint > 0.0:
            exceeded = self._check_budget_estimate(node, opts)
            if exceeded:
                stack.pop()
                self._graph.mark_halt(graph_node_id, stop_reason="budget_exceeded")
                return Decision.HALT

        # Pipeline pre-dispatch check.
        if self._pipeline is not None:
            pipeline_decision = self._check_pipeline_pre_dispatch(
                node_id, kind, opts, node, stack, graph_node_id
            )
            if pipeline_decision is not None:
                return pipeline_decision

        # CircuitBreaker pre-dispatch check.
        if self._circuit_breaker is not None:
            cb_decision = self._check_circuit_breaker(node, stack, graph_node_id)
            if cb_decision is not None:
                return cb_decision

        # Dispatch the callable.
        self._graph.mark_running(graph_node_id)
        self._forward_divergence_events(graph_node_id)

        _fn_exc, _buf_token = self._invoke_fn(fn, opts, graph_node_id)

        if _fn_exc is not None:
            return self._handle_fn_error(
                _fn_exc, node_id, opts, node, stack, graph_node_id
            )

        # Success path.
        actual_cost = self._compute_actual_cost(kind, opts)

        # Pipeline budget check (post-call, LLM calls only).
        if kind == "llm" and self._pipeline is not None and actual_cost > 0.0:
            charge_decision = self._check_before_charge(
                node_id, opts, actual_cost, node, stack, graph_node_id
            )
            if charge_decision is not None:
                return charge_decision

        return self._finalize_success(node, stack, graph_node_id, actual_cost, opts)

    # ------------------------------------------------------------------
    # _wrap sub-helpers
    # ------------------------------------------------------------------

    def _begin_graph_node(
        self,
        kind: Literal["llm", "tool"],
        opts: WrapOptions,
    ) -> tuple[list[str], str]:
        """Initialize the thread-local node stack and create a graph node.

        Returns:
            (stack, graph_node_id) where stack is the per-thread call stack.
        """
        stack: list[str] = getattr(self._node_stack, "stack", None)
        if stack is None:
            self._node_stack.stack = []
            stack = self._node_stack.stack
        graph_parent_id = stack[-1] if stack else self._root_node_id

        graph_node_id = self._graph.begin_node(
            parent_id=graph_parent_id,
            kind=kind,
            name=opts.operation_name or "unnamed",
            model=self._metadata.model if kind == "llm" else None,
        )
        stack.append(graph_node_id)
        return stack, graph_node_id

    def _halt_node(
        self,
        node: NodeRecord,
        stack: list[str],
        graph_node_id: str,
        stop_reason: str,
    ) -> Decision:
        """Record node as halted and return Decision.HALT."""
        node.status = "halted"
        node.end_ts = datetime.now(timezone.utc)
        with self._lock:
            self._nodes.append(node)
        stack.pop()
        self._graph.mark_halt(graph_node_id, stop_reason=stop_reason)
        return Decision.HALT

    def _check_budget_estimate(
        self, node: NodeRecord, opts: WrapOptions
    ) -> bool:
        """Check projected cost against ceiling. Records node and emits event if exceeded.

        Returns:
            True if budget would be exceeded (caller should halt).
        """
        with self._lock:
            projected = self._cost_usd_accumulated + opts.cost_estimate_hint
            if projected > self._config.max_cost_usd:
                self._emit_chain_event(
                    "budget_exceeded",
                    f"cost estimate ${opts.cost_estimate_hint:.4f} would exceed "
                    f"chain ceiling ${self._config.max_cost_usd:.4f}",
                )
                node.status = "halted"
                node.end_ts = datetime.now(timezone.utc)
                self._nodes.append(node)
                return True
        return False

    def _check_pipeline_pre_dispatch(
        self,
        node_id: str,
        kind: Literal["llm", "tool"],
        opts: WrapOptions,
        node: NodeRecord,
        stack: list[str],
        graph_node_id: str,
    ) -> Decision | None:
        """Run pipeline before_llm_call / before_tool_call.

        Returns:
            The pipeline Decision if it is not ALLOW, else None.
        """
        tool_ctx = self._make_tool_ctx(node_id, opts)
        if kind == "llm":
            pipeline_decision = self._pipeline.before_llm_call(tool_ctx)  # type: ignore[union-attr]
        else:
            pipeline_decision = self._pipeline.before_tool_call(tool_ctx)  # type: ignore[union-attr]

        if pipeline_decision != Decision.ALLOW:
            with self._lock:
                for ev in self._pipeline.get_events():  # type: ignore[union-attr]
                    if ev not in self._events:
                        self._events.append(ev)
            node.status = "halted"
            node.end_ts = datetime.now(timezone.utc)
            with self._lock:
                self._nodes.append(node)
            stack.pop()
            self._graph.mark_halt(graph_node_id, stop_reason="pipeline_halt")
            return pipeline_decision
        return None

    def _check_circuit_breaker(
        self,
        node: NodeRecord,
        stack: list[str],
        graph_node_id: str,
    ) -> Decision | None:
        """Run CircuitBreaker.check(). Returns Decision.HALT or None."""
        from veronica_core.runtime_policy import PolicyContext
        with self._lock:
            cost = self._cost_usd_accumulated
            step = self._step_count
        policy_ctx = PolicyContext(
            cost_usd=cost,
            step_count=step,
            chain_id=self._metadata.chain_id,
            entity_id=self._metadata.user_id or "",
        )
        pd = self._circuit_breaker.check(policy_ctx)  # type: ignore[union-attr]
        if not pd.allowed:
            with self._lock:
                self._emit_chain_event("circuit_open", f"circuit breaker denied: {pd.reason}")
            node.status = "halted"
            node.end_ts = datetime.now(timezone.utc)
            with self._lock:
                self._nodes.append(node)
            stack.pop()
            self._graph.mark_halt(graph_node_id, stop_reason="circuit_open")
            return Decision.HALT
        return None

    def _forward_divergence_events(self, graph_node_id: str) -> None:
        """Drain divergence events from ExecutionGraph and append to chain log.

        Divergence does NOT halt execution; events are informational only.
        """
        for div_evt in self._graph.drain_divergence_events():
            event_type = div_evt.get("event_type", "divergence_suspected")
            if "signature" in div_evt:
                reason = (
                    f"repeated {div_evt['signature'][0]}/{div_evt['signature'][1]}"
                    f" x{div_evt['repeat_count']}"
                )
                metadata: dict[str, Any] = {
                    "signature": div_evt["signature"],
                    "repeat_count": div_evt["repeat_count"],
                    "chain_id": div_evt["chain_id"],
                    "last_node_id": graph_node_id,
                }
            else:
                reason = event_type
                metadata = {**div_evt, "last_node_id": graph_node_id}
            safe_evt = SafetyEvent(
                event_type=event_type,
                decision=Decision.ALLOW,
                reason=reason,
                hook="ExecutionGraph",
                request_id=self._metadata.request_id,
                metadata=metadata,
            )
            with self._lock:
                if safe_evt not in self._events:
                    self._events.append(safe_evt)

    def _invoke_fn(
        self,
        fn: Callable[[], Any],
        opts: WrapOptions,
        graph_node_id: str,
    ) -> tuple["BaseException | None", "Any"]:
        """Execute fn() under partial buffer context.

        Returns:
            (exception_or_None, buf_token) where buf_token is the ContextVar
            token for the partial buffer (already reset on return).
        """
        _buf_token = None
        if opts.partial_buffer is not None:
            self._partial_buffers[graph_node_id] = opts.partial_buffer
            _buf_token = _current_partial_buffer.set(opts.partial_buffer)

        _fn_exc: BaseException | None = None
        try:
            fn()
        except BaseException as exc:
            _fn_exc = exc
        finally:
            if _buf_token is not None:
                _current_partial_buffer.reset(_buf_token)
        return _fn_exc, _buf_token

    def _handle_fn_error(
        self,
        exc: BaseException,
        node_id: str,
        opts: WrapOptions,
        node: NodeRecord,
        stack: list[str],
        graph_node_id: str,
    ) -> Decision:
        """Handle an exception raised by fn(). Returns HALT or RETRY."""
        if self._cancellation_token.is_cancelled:
            node.status = "timeout"
            node.end_ts = datetime.now(timezone.utc)
            with self._lock:
                self._nodes.append(node)
            stack.pop()
            self._graph.mark_halt(graph_node_id, stop_reason="timeout")
            return Decision.HALT

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

    def _compute_actual_cost(
        self, kind: Literal["llm", "tool"], opts: WrapOptions
    ) -> float:
        """Determine the actual cost for this call.

        Uses cost_estimate_hint when provided; otherwise attempts auto-pricing
        from opts.response_hint for LLM calls.

        Returns:
            Actual cost in USD (0.0 if not determinable).
        """
        actual_cost = opts.cost_estimate_hint
        if actual_cost == 0.0 and kind == "llm":
            from veronica_core.pricing import estimate_cost_usd, extract_usage_from_response
            model_name = opts.model or self._metadata.model or ""
            usage = None
            if opts.response_hint is not None:
                usage = extract_usage_from_response(opts.response_hint)
            if usage is not None:
                actual_cost = estimate_cost_usd(model_name, usage[0], usage[1])
            elif model_name:
                _ev = SafetyEvent(
                    event_type="COST_ESTIMATION_SKIPPED",
                    decision=Decision.ALLOW,
                    reason=(
                        f"model={model_name!r} known but response_hint not provided; "
                        f"cost_usd=0.0 recorded"
                    ),
                    hook="AutoPricing",
                )
                with self._lock:
                    self._events.append(_ev)
        return actual_cost

    def _check_before_charge(
        self,
        node_id: str,
        opts: WrapOptions,
        actual_cost: float,
        node: NodeRecord,
        stack: list[str],
        graph_node_id: str,
    ) -> Decision | None:
        """Run pipeline.before_charge(). Returns non-ALLOW decision or None."""
        tool_ctx = self._make_tool_ctx(node_id, opts, cost_usd=actual_cost)
        charge_decision = self._pipeline.before_charge(tool_ctx, actual_cost)  # type: ignore[union-attr]
        if charge_decision != Decision.ALLOW:
            with self._lock:
                for ev in self._pipeline.get_events():  # type: ignore[union-attr]
                    if ev not in self._events:
                        self._events.append(ev)
            node.status = "halted"
            node.end_ts = datetime.now(timezone.utc)
            with self._lock:
                self._nodes.append(node)
            stack.pop()
            self._graph.mark_halt(graph_node_id, stop_reason="before_charge_halt")
            return charge_decision
        return None

    def _finalize_success(
        self,
        node: NodeRecord,
        stack: list[str],
        graph_node_id: str,
        actual_cost: float,
        opts: WrapOptions,
    ) -> Decision:
        """Record successful completion: update counters, propagate cost, mark graph."""
        if self._circuit_breaker is not None:
            self._circuit_breaker.record_success()

        with self._lock:
            self._step_count += 1
            self._budget_backend.add(actual_cost)
            self._cost_usd_accumulated += actual_cost
            node.cost_usd = actual_cost
            node.status = "ok"
            node.end_ts = datetime.now(timezone.utc)
            self._nodes.append(node)

            if self._pipeline is not None:
                for ev in self._pipeline.get_events():
                    if ev not in self._events:
                        self._events.append(ev)

        if self._parent is not None and actual_cost > 0.0:
            self._parent._propagate_child_cost(actual_cost)

        stack.pop()
        self._graph.mark_success(graph_node_id, cost_usd=actual_cost)

        if opts.partial_buffer is not None:
            opts.partial_buffer.mark_complete()

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

    def _propagate_child_cost(self, cost_usd: float) -> None:
        """Receive cost from a child context and accumulate it here.

        If accumulated cost exceeds ceiling, marks context as aborted.
        Propagates further if this context also has a parent.

        Args:
            cost_usd: Cost amount (in USD) spent by the child context.
        """
        with self._lock:
            self._cost_usd_accumulated += cost_usd
            if self._cost_usd_accumulated >= self._config.max_cost_usd:
                self._emit_chain_event(
                    "budget_exceeded_by_child",
                    f"child propagation pushed chain total "
                    f"${self._cost_usd_accumulated:.4f} >= "
                    f"ceiling ${self._config.max_cost_usd:.4f}",
                )
                self._aborted = True
        # Propagate further up if we have a parent (outside lock to avoid deadlock).
        if self._parent is not None:
            self._parent._propagate_child_cost(cost_usd)

    def spawn_child(
        self,
        max_cost_usd: float | None = None,
        max_steps: int | None = None,
        max_retries_total: int | None = None,
        timeout_ms: int = 0,
        pipeline: "ShieldPipeline | None" = None,
    ) -> "ExecutionContext":
        """Create a child context whose costs propagate to this parent.

        Args:
            max_cost_usd: Child ceiling. Defaults to parent's remaining budget.
            max_steps: Child step limit. Defaults to parent's.
            max_retries_total: Child retry budget. Defaults to parent's.
            timeout_ms: Child timeout. 0 = no timeout.
            pipeline: Optional ShieldPipeline for child.

        Returns:
            A new ExecutionContext linked to this parent.

        Example::

            with parent_ctx.spawn_child(max_cost_usd=0.5) as child:
                child.wrap_llm_call(agent_b_fn)
        """
        with self._lock:
            remaining = self._config.max_cost_usd - self._cost_usd_accumulated
        child_max = max_cost_usd if max_cost_usd is not None else remaining
        child_cfg = ExecutionConfig(
            max_cost_usd=max(0.0, child_max),
            max_steps=max_steps if max_steps is not None else self._config.max_steps,
            max_retries_total=(
                max_retries_total
                if max_retries_total is not None
                else self._config.max_retries_total
            ),
            timeout_ms=timeout_ms,
        )
        return ExecutionContext(config=child_cfg, pipeline=pipeline, parent=self)

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
        # Dedup by content fields (excluding ts, which is unique per-instance).
        # SafetyEvent.ts is auto-set to datetime.now() on construction, so two
        # events with identical fields but different creation times would NOT be
        # equal under the default frozen-dataclass __eq__. Compare by key tuple
        # instead to correctly suppress duplicate limit-check emissions.
        dedup_key = (event.event_type, event.decision, event.reason, event.hook, event.request_id)
        if len(self._events) < _MAX_CHAIN_EVENTS and not any(
            (e.event_type, e.decision, e.reason, e.hook, e.request_id) == dedup_key
            for e in self._events
        ):
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
