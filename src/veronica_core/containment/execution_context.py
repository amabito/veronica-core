# Refactored: limit-checking extracted to _limit_checker.py, event-log to _chain_event_log.py
"""ExecutionContext -- chain-level containment for VERONICA agent runs.

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
# v0.11 -- WrapOptions.partial_buffer field; _current_partial_buffer ContextVar;
#          get_current_partial_buffer(); ExecutionContext.get_partial_result().
# ---------------------------------------------------------------------------

from __future__ import annotations

import contextvars
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Literal, TYPE_CHECKING

from veronica_core.containment._chain_event_log import _ChainEventLog
from veronica_core.containment._limit_checker import _LimitChecker
from veronica_core.containment.execution_graph import ExecutionGraph
from veronica_core.containment.types import (
    CancellationToken,
    ChainMetadata,
    ContextSnapshot,
    ExecutionConfig,
    NodeRecord,
    WrapOptions,
)
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision, ToolCallContext

if TYPE_CHECKING:
    from veronica_core.a2a.types import AgentIdentity
    from veronica_core.circuit_breaker import CircuitBreaker
    from veronica_core.containment.budget_allocator import BudgetAllocator
    from veronica_core.memory.governor import MemoryGovernor
    from veronica_core.memory.types import MemoryOperation
    from veronica_core.policy.frozen_view import PolicyViewHolder
    from veronica_core.shield.pipeline import ShieldPipeline
    from veronica_core.partial import PartialResultBuffer

logger = logging.getLogger(__name__)

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

    Must be called from inside a fn() callback passed to wrap_llm_call().
    Calling the same buffer again is idempotent.

    Raises RuntimeError if a different buffer is already attached to the current
    context (prevents silent overwrite of an in-progress partial capture).
    """
    current = _current_partial_buffer.get()
    if current is not None and current is not buf:
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
# ExecutionContext
# ---------------------------------------------------------------------------

# Maximum number of NodeRecords stored per chain. Prevents unbounded growth in
# long-running agents or run-away loops that generate many nodes.
_MAX_NODES: int = 10_000

# Maximum number of partial-buffer entries. Each entry holds a reference to a
# PartialResultBuffer object; cap prevents unbounded dict growth.
_MAX_PARTIAL_BUFFERS: int = 1_000


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
        metrics: Any = None,
        agent_identity: "AgentIdentity | None" = None,
        memory_governor: "MemoryGovernor | None" = None,
        policy_view_holder: "PolicyViewHolder | None" = None,
    ) -> None:
        self._config = config
        self._pipeline = pipeline
        self._circuit_breaker = circuit_breaker
        # ContainmentMetricsProtocol-compatible object, or None for zero overhead.
        self._metrics = metrics
        self._agent_identity: AgentIdentity | None = agent_identity
        # Memory governance -- optional chain-level memory operation gate (v3.3).
        self._memory_governor: MemoryGovernor | None = memory_governor
        # Policy view holder -- active policy metadata for audit enrichment (v3.3).
        self._policy_view_holder: PolicyViewHolder | None = policy_view_holder
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

        # Outer lock for operations that span multiple helpers (e.g. _wrap reads
        # from _limits AND appends nodes).  Each helper also has its own internal
        # lock for its own state.
        self._lock = threading.Lock()
        self._closed: bool = False
        self._nodes: list[NodeRecord] = []

        # Initialise CancellationToken before _LimitChecker so the token is
        # available when passed to the checker.
        self._cancellation_token = CancellationToken()

        # Delegated helpers: limit counters and chain event log.
        self._limits = _LimitChecker(config, self._cancellation_token)
        self._event_log = _ChainEventLog()

        # Pre-built emit callback for _check_limits_delegate (avoid per-call lambda).
        self._emit_chain_event_cb = self._make_emit_chain_event_cb()

        # Execution graph for DAG tracking of all nodes.
        self._graph = ExecutionGraph(chain_id=self._metadata.chain_id)
        self._root_node_id = self._graph.create_root("chain_root", {})
        # ContextVar-backed stack for nested parent tracking.
        # Design: the ContextVar stores a list[str] that is lazily created per
        # context on first use.  The _nesting_depth_var tracks how many _wrap()
        # calls are currently in flight *in this context*; it starts at 0 for any
        # context (including asyncio tasks that inherit a copy) and increments on
        # entry / decrements on exit.  A depth of 0 means no wrap is active, so
        # _begin_graph_node must create a fresh list even if a non-None list was
        # inherited via context copy (K: asyncio task isolation fix).
        self._node_stack_var: contextvars.ContextVar[list[str] | None] = (
            contextvars.ContextVar(
                f"veronica_node_stack_{self._metadata.chain_id[:8]}",
                default=None,
            )
        )
        # Tracks active nesting depth per context. Always starts at 0 for a new
        # context, even if the list was inherited.  This is the invariant that
        # distinguishes "first wrap in this context" from "nested wrap".
        self._nesting_depth_var: contextvars.ContextVar[int] = contextvars.ContextVar(
            f"veronica_nesting_depth_{self._metadata.chain_id[:8]}",
            default=0,
        )

        # Partial buffers keyed by graph_node_id. Populated when WrapOptions.partial_buffer
        # is set; used by get_partial_result() to look up partial text per node.
        self._partial_buffers: dict[str, PartialResultBuffer] = {}

        if config.timeout_ms > 0:
            self._limits.timeout.start_watcher(
                timeout_ms=config.timeout_ms,
                emit_fn=self._emit_chain_event,
                config_timeout_ms=config.timeout_ms,
            )

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
        self.close()

    @property
    def agent_identity(self) -> "AgentIdentity | None":
        """Return the agent identity associated with this context, or None."""
        return self._agent_identity

    # ------------------------------------------------------------------
    # Internal compatibility shims (used by tests that access private state)
    # These proxy directly to the _LimitChecker helper.
    # ------------------------------------------------------------------

    @property
    def _aborted(self) -> bool:
        return self._limits.is_aborted

    @_aborted.setter
    def _aborted(self, value: bool) -> None:  # noqa: FBT001
        # Only used in legacy tests; setting False is silently ignored (safe).
        if value:
            self._limits.mark_aborted("external_set")

    @property
    def _cost_usd_accumulated(self) -> float:
        return self._limits.cost_usd_accumulated

    @_cost_usd_accumulated.setter
    def _cost_usd_accumulated(self, value: float) -> None:
        # Tests directly assign this to set up specific scenarios.
        self._limits.set_cost(value)

    @property
    def _step_count(self) -> int:
        return self._limits.step_count

    @_step_count.setter
    def _step_count(self, value: int) -> None:
        # External code (adapters, tests) may increment _step_count directly.
        self._limits.set_step_count(value)

    @property
    def _retries_used(self) -> int:
        return self._limits.retries_used

    @property
    def _abort_reason(self) -> str | None:
        return self._limits.abort_reason

    @property
    def _events(self) -> list:
        return self._event_log.snapshot()

    @property
    def _timeout_pool_handle(self) -> object | None:
        """Return the active timeout pool handle, or None after cancellation.

        Compatibility shim for tests that inspect the timeout pool handle
        directly.  The handle is owned by TimeoutManager.
        """
        return self._limits.timeout._pool_handle

    @_timeout_pool_handle.setter
    def _timeout_pool_handle(self, value: object | None) -> None:
        # Legacy setter: tests may clear this to None; forward to TimeoutManager.
        with self._limits.timeout._lock:
            self._limits.timeout._pool_handle = value

    def _increment_step_returning(self) -> int:
        """Atomically increment step count and return new value.

        Used by adapter proxies to avoid read-modify-write through property shims.
        """
        return self._limits.increment_step_returning()

    def _add_cost_returning(self, amount: float) -> float:
        """Atomically add cost and return new accumulated total.

        Used by adapter proxies to avoid read-modify-write through property shims.
        """
        return self._limits.add_cost_returning(amount)

    def close(self) -> None:
        """Release resources held by this context.

        Cancels any pending timeout, clears partial buffers, and marks the
        context as closed.  Subsequent calls to wrap_llm_call / wrap_tool_call
        return Decision.HALT immediately after close() is called.

        Idempotent: calling close() more than once is safe and produces no
        additional side-effects beyond the first call.

        Emits a warning when called while graph nodes are still in a
        non-terminal state (running wrap calls that have not yet finished).
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True

        # Mark aborted so subsequent wrap calls return HALT immediately.
        # _limits.mark_closed() is idempotent and uses its own lock.
        self._limits.mark_closed()

        with self._lock:
            # Warn when non-terminal graph nodes exist (in-flight wrap calls).
            try:
                _non_terminal = [
                    nid
                    for nid, node in self._graph._nodes.items()
                    if node.status not in ("success", "fail", "halt")
                    and nid != self._root_node_id
                ]
                if _non_terminal:
                    logger.warning(
                        "ExecutionContext.close() called while %d graph node(s) are "
                        "still in non-terminal state: %s",
                        len(_non_terminal),
                        _non_terminal[:5],
                    )
            except Exception:
                # Intentionally swallowed: the non-terminal node check is
                # diagnostic only; a failure here must not prevent close() from
                # completing its cleanup work.
                pass

            # Clear partial buffers to release references.
            self._partial_buffers.clear()

        # Signal cancellation token and cancel the scheduled timeout callback
        # (outside lock to avoid deadlock if the timeout callback tries to
        # acquire _lock while we hold it).
        self._cancellation_token.cancel()
        self._limits.timeout.cancel_watcher()

        if hasattr(self, "_budget_backend"):
            try:
                self._budget_backend.close()
            except Exception:
                logger.debug(
                    "ExecutionContext.close(): budget_backend.close() failed",
                    exc_info=True,
                )
        if hasattr(self, "_circuit_breaker") and hasattr(
            self._circuit_breaker, "close"
        ):
            try:
                self._circuit_breaker.close()
            except Exception:
                logger.debug(
                    "ExecutionContext.close(): circuit_breaker.close() failed",
                    exc_info=True,
                )

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

    _VALID_MEMORY_KINDS: frozenset[str] = frozenset({"memory_read", "memory_write"})

    def wrap_memory_call(
        self,
        fn: Callable[[], Any],
        kind: Literal["memory_read", "memory_write"],
        options: WrapOptions | None = None,
    ) -> Decision:
        """Execute *fn* under chain-level containment (memory variant).

        Memory calls count toward the step budget identically to tool calls.
        They do NOT go through ShieldPipeline.before_tool_call -- memory
        operations are not subject to the tool-call hook chain.
        They DO go through MemoryGovernor when one is configured.

        Args:
            fn: Zero-argument callable representing the memory operation.
            kind: "memory_read" for read operations, "memory_write" for writes.
            options: Optional per-call configuration.

        Returns:
            Decision.ALLOW on clean completion.
            Decision.HALT when any chain-level limit is exceeded or the
            MemoryGovernor denies the operation.

        Raises:
            ValueError: If *kind* is not "memory_read" or "memory_write".
        """
        if kind not in self._VALID_MEMORY_KINDS:
            raise ValueError(
                f"wrap_memory_call() kind must be 'memory_read' or 'memory_write', "
                f"got {kind!r}"
            )
        return self._wrap(fn, kind=kind, options=options)

    def record_event(self, event: SafetyEvent) -> None:
        """Append *event* to the chain-level event log.

        Use this when application code emits SafetyEvent instances outside
        of wrap_llm_call / wrap_tool_call.

        Silently drops events once the internal cap (_MAX_CHAIN_EVENTS) is
        reached to prevent memory exhaustion from flooding callers.

        Args:
            event: SafetyEvent to record.
        """
        self._event_log.append(event)

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
        counters = self._limits.snapshot_counters()
        with self._lock:
            nodes_copy = list(self._nodes)
            parent_chain_id = (
                self._parent._metadata.chain_id if self._parent is not None else None
            )
            graph_snap = self._graph.snapshot()
        graph_summary = graph_snap["aggregates"]
        return ContextSnapshot(
            chain_id=self._metadata.chain_id,
            request_id=self._metadata.request_id,
            step_count=counters["step_count"],
            cost_usd_accumulated=counters["cost_usd_accumulated"],
            retries_used=counters["retries_used"],
            aborted=counters["aborted"],
            abort_reason=counters["abort_reason"],
            elapsed_ms=counters["elapsed_ms"],
            nodes=nodes_copy,
            events=self._event_log.snapshot(),
            graph_summary=graph_summary,
            parent_chain_id=parent_chain_id,
            agent_identity=self._agent_identity,
            policy_metadata=self._get_policy_audit_metadata(),
        )

    def get_graph_snapshot(self) -> dict[str, Any]:
        """Return the full ExecutionGraph snapshot as a JSON-serializable dict.

        Contains all nodes, aggregates, and chain metadata.

        Returns:
            dict as produced by ExecutionGraph.snapshot().
        """
        # L6: Acquire _lock for consistency with get_snapshot() which also reads
        # graph state while holding the lock.
        with self._lock:
            return self._graph.snapshot()

    def get_partial_result(self, node_id: str) -> "PartialResultBuffer | None":
        """Return the PartialResultBuffer for *node_id*, or None if none was attached.

        Args:
            node_id: The graph_node_id associated with the wrap call.
        """
        with self._lock:
            return self._partial_buffers.get(node_id)

    def abort(self, reason: str) -> None:
        """Cancel all pending work and prevent future wrap calls.

        Idempotent. Subsequent calls to wrap_llm_call / wrap_tool_call
        return Decision.HALT immediately without executing the callable.

        Does not raise an exception.

        Args:
            reason: Human-readable explanation recorded in the snapshot.
        """
        if self._limits.mark_aborted(reason):
            self._cancellation_token.cancel()
            self._emit_chain_event("aborted", reason)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _wrap(
        self,
        fn: Callable[[], Any],
        kind: Literal["llm", "tool", "memory_read", "memory_write"],
        options: WrapOptions | None,
    ) -> Decision:
        """Common implementation for wrap_llm_call, wrap_tool_call, and wrap_memory_call."""
        opts = options or WrapOptions()
        node_id = str(uuid.uuid4())

        with self._lock:
            parent_id = self._nodes[-1].node_id if self._nodes else None

        # H5: parent_id is read under lock above, but _begin_graph_node is called
        # outside the lock below.  A concurrent thread can append to _nodes between
        # the two points, causing parent_id to lag one node behind the true tail.
        # This is intentionally benign: graph parent linkage is used for diagnostic
        # tracing only, not for any correctness or safety decision.  Extending the
        # lock to cover _begin_graph_node is not warranted because _begin_graph_node
        # may call pipeline hooks that acquire their own locks (deadlock risk).
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

        # _begin_graph_node increments _nesting_depth_var. Move into the try block
        # so the finally clause always decrements depth, even if begin_node()
        # raises after the increment (L: depth-leak guard).
        # Initialise to safe defaults so the except/finally branches can reference
        # these names unconditionally even if _begin_graph_node raises before
        # returning.
        stack: list[str] = []
        graph_node_id: str = ""
        # reservation_id is set when the backend supports two-phase accounting.
        # It is committed on success and rolled back on any failure/exception.
        _reservation_id: str | None = None

        try:
            stack, graph_node_id = self._begin_graph_node(kind, opts)

            # Pre-flight: chain-level limit check.
            halt_reason = self._check_limits_delegate()
            if halt_reason is not None:
                return self._halt_node(node, stack, graph_node_id, halt_reason)

            # Pre-flight: cost estimate check (before calling fn).
            # If the backend supports reserve/commit/rollback, use two-phase accounting
            # to atomically hold escrow and prevent TOCTOU overspend.
            if opts.cost_estimate_hint > 0.0:
                if hasattr(self._budget_backend, "reserve"):
                    try:
                        _reservation_id = self._budget_backend.reserve(
                            opts.cost_estimate_hint, self._config.max_cost_usd
                        )
                    except OverflowError:
                        reason = (
                            f"cost estimate ${opts.cost_estimate_hint:.4f} would exceed "
                            f"chain ceiling ${self._config.max_cost_usd:.4f}"
                        )
                        self._emit_chain_event("budget_exceeded", reason)
                        return self._halt_node(
                            node,
                            stack,
                            graph_node_id,
                            reason,
                        )
                else:
                    exceeded = self._check_budget_estimate(node, opts)
                    if exceeded:
                        stack.pop()
                        self._graph.mark_halt(
                            graph_node_id, stop_reason="budget_exceeded"
                        )
                        return Decision.HALT

            # Pipeline pre-dispatch check.
            if self._pipeline is not None:
                pipeline_decision = self._check_pipeline_pre_dispatch(
                    node_id, kind, opts, node, stack, graph_node_id
                )
                if pipeline_decision is not None:
                    self._try_rollback(_reservation_id)
                    return pipeline_decision

            # CircuitBreaker pre-dispatch check.
            if self._circuit_breaker is not None:
                cb_decision = self._check_circuit_breaker(node, stack, graph_node_id)
                if cb_decision is not None:
                    self._try_rollback(_reservation_id)
                    return cb_decision

            # Memory governance pre-dispatch check (v3.3).
            if self._memory_governor is not None:
                mg_decision = self._check_memory_governance(
                    kind, opts, node, stack, graph_node_id
                )
                if mg_decision is not None:
                    self._try_rollback(_reservation_id)
                    return mg_decision

            # Dispatch the callable.
            self._graph.mark_running(graph_node_id)
            self._forward_divergence_events(graph_node_id)

            _fn_exc, _buf_token = self._invoke_fn(fn, opts, graph_node_id)

            if _fn_exc is not None:
                self._try_rollback(_reservation_id)
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
                    self._try_rollback(_reservation_id)
                    return charge_decision

            return self._finalize_success(
                node, stack, graph_node_id, actual_cost, opts, _reservation_id
            )

        except BaseException:
            # Ensure the graph stack is popped and the node is closed even when an
            # unexpected exception escapes all sub-helpers (e.g. a hook that raises
            # after the stack was pushed but before the normal pop path is reached).
            if stack and graph_node_id in stack:
                try:
                    stack.remove(graph_node_id)
                except ValueError:
                    pass
                try:
                    self._graph.mark_failure(
                        graph_node_id, error_class="UnexpectedException"
                    )
                except Exception:
                    # Intentionally swallowed: graph bookkeeping failure must
                    # not mask the original exception being re-raised.
                    pass
            if node.end_ts is None:
                node.end_ts = datetime.now(timezone.utc)
                node.status = "error"
            # Roll back any pending reservation to prevent budget leak.
            self._try_rollback(_reservation_id)
            raise
        finally:
            # Decrement nesting depth so the next outermost _wrap() call in this
            # context sees depth==0 and creates a fresh stack if needed (K fix).
            d = self._nesting_depth_var.get()
            if d > 0:
                self._nesting_depth_var.set(d - 1)

    def _try_rollback(self, reservation_id: str | None) -> None:
        """Roll back a reservation against the configured backend, swallowing all exceptions."""
        if reservation_id is None:
            return
        try:
            self._budget_backend.rollback(reservation_id)
        except Exception:  # noqa: BLE001
            logger.debug(
                "ExecutionContext: budget_backend.rollback(%r) failed; reservation may leak",
                reservation_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # _wrap sub-helpers
    # ------------------------------------------------------------------

    def _begin_graph_node(
        self,
        kind: Literal["llm", "tool", "memory_read", "memory_write"],
        opts: WrapOptions,
    ) -> tuple[list[str], str]:
        """Initialize the ContextVar node stack and create a graph node.

        Uses _nesting_depth_var to distinguish two cases:
        - depth == 0: first wrap in this context (including asyncio tasks that
          inherited a non-None list via copy_context). Always create a fresh list
          to prevent cross-task stack contamination (K: async safety fix).
        - depth > 0: nested wrap within the same active context. Reuse the list
          that was created by the outermost wrap at depth==0.

        The depth counter is incremented here and decremented in _wrap() via
        a try/finally so that it is always restored on every exit path.

        Returns:
            (stack, graph_node_id) where stack is the per-context call stack.
        """
        depth = self._nesting_depth_var.get()
        stack: list[str] | None = self._node_stack_var.get()
        if depth == 0 or stack is None:
            # First wrap in this context -- start with a fresh list regardless of
            # any inherited non-None reference (fixes asyncio context-copy sharing).
            stack = []
            self._node_stack_var.set(stack)
        # Depth incremented here; _wrap() decrements in its finally block.
        self._nesting_depth_var.set(depth + 1)
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
            if len(self._nodes) < _MAX_NODES:
                self._nodes.append(node)
            else:
                logger.warning(
                    "ExecutionContext: _nodes cap (%d) reached; node %s will not be recorded",
                    _MAX_NODES,
                    node.node_id,
                )
        stack.pop()
        self._graph.mark_halt(graph_node_id, stop_reason=stop_reason)
        if self._metrics is not None:
            try:
                self._metrics.record_decision(self._metadata.chain_id, "HALT")
            except Exception:
                logger.debug(
                    "ExecutionContext: metrics.record_decision failed in _halt_node",
                    exc_info=True,
                )
        return Decision.HALT

    def _check_budget_estimate(self, node: NodeRecord, opts: WrapOptions) -> bool:
        """Check projected cost against ceiling. Records node and emits event if exceeded.

        Returns:
            True if budget would be exceeded (caller should halt).
        """
        projected = self._limits.cost_usd_accumulated + opts.cost_estimate_hint
        if projected > self._config.max_cost_usd:
            self._emit_chain_event(
                "budget_exceeded",
                f"cost estimate ${opts.cost_estimate_hint:.4f} would exceed "
                f"chain ceiling ${self._config.max_cost_usd:.4f}",
            )
            node.status = "halted"
            node.end_ts = datetime.now(timezone.utc)
            with self._lock:
                if len(self._nodes) < _MAX_NODES:
                    self._nodes.append(node)
            if self._metrics is not None:
                try:
                    self._metrics.record_decision(self._metadata.chain_id, "HALT")
                except Exception:
                    logger.debug(
                        "ExecutionContext: metrics.record_decision failed in _check_budget_estimate",
                        exc_info=True,
                    )
            return True
        return False

    def _check_pipeline_pre_dispatch(
        self,
        node_id: str,
        kind: Literal["llm", "tool", "memory_read", "memory_write"],
        opts: WrapOptions,
        node: NodeRecord,
        stack: list[str],
        graph_node_id: str,
    ) -> Decision | None:
        """Run pipeline before_llm_call / before_tool_call.

        Memory calls (kind="memory_read" or kind="memory_write") skip the
        ShieldPipeline entirely -- they are governed by MemoryGovernor only.

        Returns:
            The pipeline Decision if it is not ALLOW, else None.
        """
        if kind in ("memory_read", "memory_write"):
            # Memory operations bypass ShieldPipeline hook chain.
            return None
        tool_ctx = self._make_tool_ctx(node_id, opts)
        if kind == "llm":
            pipeline_decision = self._pipeline.before_llm_call(tool_ctx)  # type: ignore[union-attr]
        else:
            pipeline_decision = self._pipeline.before_tool_call(tool_ctx)  # type: ignore[union-attr]

        if pipeline_decision != Decision.ALLOW:
            # M6: Collect pipeline events outside lock to avoid potential
            # re-entrant deadlock if pipeline hooks call back into this context.
            pre_halt_events = self._pipeline.get_events()  # type: ignore[union-attr]
            node.status = "halted"
            node.end_ts = datetime.now(timezone.utc)
            self._event_log.append_batch(pre_halt_events)
            with self._lock:
                if len(self._nodes) < _MAX_NODES:
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

        cost = self._limits.cost_usd_accumulated
        step = self._limits.step_count
        policy_ctx = PolicyContext(
            cost_usd=cost,
            step_count=step,
            chain_id=self._metadata.chain_id,
            entity_id=self._metadata.user_id or "",
        )
        pd = self._circuit_breaker.check(policy_ctx)  # type: ignore[union-attr]
        if not pd.allowed:
            self._emit_chain_event(
                "circuit_open",
                f"circuit breaker denied: {pd.reason}",
            )
            node.status = "halted"
            node.end_ts = datetime.now(timezone.utc)
            with self._lock:
                if len(self._nodes) < _MAX_NODES:
                    self._nodes.append(node)
            stack.pop()
            self._graph.mark_halt(graph_node_id, stop_reason="circuit_open")
            if self._metrics is not None:
                try:
                    self._metrics.record_circuit_state(
                        self._metadata.chain_id,
                        self._circuit_breaker.state.value,  # type: ignore[union-attr]
                    )
                except Exception:
                    logger.debug(
                        "ExecutionContext: metrics.record_circuit_state failed",
                        exc_info=True,
                    )
            return Decision.HALT
        return None

    _MG_DENIED = "memory_governance_denied"

    def _build_memory_op(
        self,
        kind: Literal["llm", "tool", "memory_read", "memory_write"],
        opts: WrapOptions,
    ) -> "MemoryOperation":
        """Build a MemoryOperation from the current wrap call context.

        memory_read -> MemoryAction.READ
        memory_write -> MemoryAction.WRITE
        llm -> MemoryAction.WRITE (legacy: LLM calls are treated as writes)
        tool -> MemoryAction.READ (legacy: tool calls are treated as reads)
        """
        from veronica_core.memory.types import MemoryAction, MemoryOperation

        if kind == "memory_read":
            action = MemoryAction.READ
        elif kind == "memory_write":
            action = MemoryAction.WRITE
        elif kind == "llm":
            action = MemoryAction.WRITE
        elif kind == "tool":
            action = MemoryAction.READ
        else:
            raise AssertionError(f"unreachable kind: {kind!r}")
        return MemoryOperation(
            action=action,
            agent_id=self._metadata.user_id or "",
            namespace=self._metadata.chain_id,
            content_size_bytes=0,  # TODO(v3.x): replace with actual token/byte count
            metadata={"operation_name": opts.operation_name, "kind": kind},
        )

    def _check_memory_governance(
        self,
        kind: Literal["llm", "tool", "memory_read", "memory_write"],
        opts: WrapOptions,
        node: NodeRecord,
        stack: list[str],
        graph_node_id: str,
    ) -> Decision | None:
        """Evaluate memory governance before dispatch. Returns Decision.HALT or None.

        Builds a MemoryOperation from the wrap call context and evaluates it
        against the chain-level MemoryGovernor. If denied, emits a chain event
        and halts the node.

        Only DENY verdicts halt dispatch. QUARANTINE and DEGRADE are treated as
        "allow with annotation" -- the dispatch proceeds and hooks can observe
        the verdict via ``notify_after``.  This is intentional: quarantine and
        degradation are post-processing concerns, not pre-dispatch gates.
        """
        from veronica_core.memory.types import MemoryPolicyContext

        mem_op = self._build_memory_op(kind, opts)
        mem_ctx = MemoryPolicyContext(
            operation=mem_op,
            chain_id=self._metadata.chain_id,
            request_id=self._metadata.request_id,
            total_memory_ops_in_chain=self._limits.step_count,
            total_bytes_written_in_chain=0,
        )
        try:
            decision = self._memory_governor.evaluate(mem_op, mem_ctx)  # type: ignore[union-attr]
            if decision is None:
                raise TypeError("MemoryGovernor.evaluate() returned None")
        except Exception as exc:  # noqa: BLE001
            # Fail-closed: governor error -> deny.
            logger.error(
                "ExecutionContext: MemoryGovernor.evaluate() raised: %s", exc
            )
            self._emit_chain_event(
                self._MG_DENIED,
                f"memory governor error: {type(exc).__name__}",
            )
            return self._halt_node(
                node, stack, graph_node_id, self._MG_DENIED
            )

        if decision.denied:
            self._emit_chain_event(
                self._MG_DENIED,
                f"memory governance denied: {decision.reason} "
                f"(policy={decision.policy_id})",
            )
            return self._halt_node(
                node, stack, graph_node_id, self._MG_DENIED
            )
        return None

    def _notify_memory_governance_after(
        self,
        kind: Literal["llm", "tool", "memory_read", "memory_write"],
        opts: WrapOptions,
    ) -> None:
        """Notify memory governor after successful dispatch. Never raises."""
        from veronica_core.memory.types import (
            GovernanceVerdict,
            MemoryGovernanceDecision,
        )

        mem_op = self._build_memory_op(kind, opts)
        allow_decision = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="post-dispatch notification",
            policy_id="execution_context",
            operation=mem_op,
        )
        try:
            self._memory_governor.notify_after(  # type: ignore[union-attr]
                mem_op, allow_decision
            )
        except BaseException:  # noqa: BLE001
            # Catch BaseException (not just Exception) to honour the "never
            # raises" contract.  SystemExit/KeyboardInterrupt from a governance
            # hook after_op must not corrupt a successfully completed node --
            # cost has already been committed and the graph node finalised.
            logger.debug(
                "ExecutionContext: MemoryGovernor.notify_after() raised unexpectedly",
                exc_info=True,
            )

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
            self._event_log.append(safe_evt)

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
            # M2: Protect _partial_buffers dict write under _lock to prevent
            # concurrent writes from different threads racing on the same dict.
            with self._lock:
                if len(self._partial_buffers) < _MAX_PARTIAL_BUFFERS:
                    self._partial_buffers[graph_node_id] = opts.partial_buffer
                else:
                    logger.warning(
                        "ExecutionContext: _partial_buffers cap (%d) reached; "
                        "partial buffer for node %s will not be tracked",
                        _MAX_PARTIAL_BUFFERS,
                        graph_node_id,
                    )
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
                if len(self._nodes) < _MAX_NODES:
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
            self._circuit_breaker.record_failure(error=exc)
            if self._metrics is not None:
                try:
                    self._metrics.record_circuit_state(
                        self._metadata.chain_id,
                        self._circuit_breaker.state.value,
                    )
                except Exception:
                    logger.debug(
                        "ExecutionContext: metrics.record_circuit_state failed",
                        exc_info=True,
                    )

        node.status = "error"
        node.end_ts = datetime.now(timezone.utc)
        with self._lock:
            if len(self._nodes) < _MAX_NODES:
                self._nodes.append(node)
        stack.pop()
        self._graph.mark_failure(graph_node_id, error_class=type(exc).__name__)

        # Re-raise signal-class exceptions (KeyboardInterrupt, SystemExit) after
        # node bookkeeping is complete.  These must propagate to the caller; storing
        # them as a Decision would silently swallow process-termination signals.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise exc

        if error_decision == Decision.HALT:
            return Decision.HALT

        # Only increment retry counters when we are actually going to retry.
        # HALT decisions should not consume the retry budget.
        self._limits.increment_retries()
        node.retries_used += 1

        return Decision.RETRY

    def _compute_actual_cost(
        self,
        kind: Literal["llm", "tool", "memory_read", "memory_write"],
        opts: WrapOptions,
    ) -> float:
        """Determine the actual cost for this call.

        Uses cost_estimate_hint when provided; otherwise attempts auto-pricing
        from opts.response_hint for LLM calls. Memory calls never trigger
        auto-pricing (they have no token usage).

        Returns:
            Actual cost in USD (0.0 if not determinable).
        """
        actual_cost = opts.cost_estimate_hint
        if actual_cost == 0.0 and kind == "llm":
            from veronica_core.pricing import (
                estimate_cost_usd,
                extract_usage_from_response,
            )

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
                self._event_log.append(_ev)
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
            # M6: Collect pipeline events outside lock to avoid potential
            # re-entrant deadlock if pipeline hooks call back into this context.
            before_charge_events = self._pipeline.get_events()  # type: ignore[union-attr]
            node.status = "halted"
            node.end_ts = datetime.now(timezone.utc)
            self._event_log.append_batch(before_charge_events)
            with self._lock:
                if len(self._nodes) < _MAX_NODES:
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
        reservation_id: str | None = None,
    ) -> Decision:
        """Record successful completion: update counters, propagate cost, mark graph."""
        if self._circuit_breaker is not None:
            self._circuit_breaker.record_success()
            if self._metrics is not None:
                try:
                    self._metrics.record_circuit_state(
                        self._metadata.chain_id,
                        self._circuit_breaker.state.value,
                    )
                except Exception:
                    logger.debug(
                        "ExecutionContext: metrics.record_circuit_state failed",
                        exc_info=True,
                    )

        # H3: Move backend.add()/commit() outside lock -- backend has its own internal
        # locking and may perform blocking Redis IO. Holding _lock during that
        # call would stall all other threads for the full round-trip latency.
        if reservation_id is not None:
            try:
                self._budget_backend.commit(reservation_id)
            except (KeyError, Exception):  # noqa: BLE001
                # Reservation expired between reserve and commit; fall back to add().
                self._budget_backend.add(actual_cost)
        else:
            self._budget_backend.add(actual_cost)

        # M6: Collect pipeline events outside lock to avoid potential re-entrant
        # deadlock if pipeline hooks call back into ExecutionContext methods.
        pipeline_events = (
            self._pipeline.get_events() if self._pipeline is not None else []
        )

        # Update limit-checker counters atomically (single lock acquisition).
        self._limits.commit_success(actual_cost)

        # Append pipeline events to the chain log (lock-safe in _event_log).
        self._event_log.append_batch(pipeline_events)

        node.cost_usd = actual_cost
        node.status = "ok"
        node.end_ts = datetime.now(timezone.utc)
        with self._lock:
            if len(self._nodes) < _MAX_NODES:
                self._nodes.append(node)
            else:
                logger.warning(
                    "ExecutionContext: _nodes cap (%d) reached; successful node %s will not be recorded",
                    _MAX_NODES,
                    node.node_id,
                )

        if self._parent is not None and actual_cost > 0.0:
            self._parent._propagate_child_cost(actual_cost)

        stack.pop()
        self._graph.mark_success(graph_node_id, cost_usd=actual_cost)

        if opts.partial_buffer is not None:
            opts.partial_buffer.mark_complete()

        if self._metrics is not None:
            _agent_id = self._metadata.chain_id
            try:
                self._metrics.record_cost(_agent_id, actual_cost)
                self._metrics.record_decision(_agent_id, "ALLOW")
                _dur = (
                    (node.end_ts - node.start_ts).total_seconds() * 1000.0
                    if node.end_ts is not None
                    else 0.0
                )
                self._metrics.record_latency(_agent_id, _dur)
                if opts.response_hint is not None:
                    from veronica_core.pricing import extract_usage_from_response

                    _usage = extract_usage_from_response(opts.response_hint)
                    if _usage is not None:
                        self._metrics.record_tokens(_agent_id, _usage[0], _usage[1])
            except Exception:
                logger.debug(
                    "ExecutionContext: metrics recording failed", exc_info=True
                )

        # Memory governance post-dispatch notification (v3.3).
        if self._memory_governor is not None:
            self._notify_memory_governance_after(node.kind, opts)

        # Reconciliation callback: notify caller of estimated vs actual cost.
        if opts.reconciliation_callback is not None:
            try:
                opts.reconciliation_callback.on_reconcile(
                    opts.cost_estimate_hint, actual_cost
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "ExecutionContext: reconciliation_callback failed", exc_info=True
                )

        return Decision.ALLOW

    def _make_emit_chain_event_cb(self) -> Any:
        """Build the emit callback once (avoids lambda allocation on every call).

        Enriches each emitted event with active policy metadata (v3.3).
        """
        request_id = self._metadata.request_id
        emit = self._event_log.emit_chain_event
        get_pm = self._get_policy_audit_metadata

        def _emit(stop_reason: str, detail: str) -> None:
            emit(stop_reason, detail, request_id, policy_metadata=get_pm())

        return _emit

    def _check_limits_delegate(self) -> str | None:
        """Return a stop-reason string if any chain-level limit is exceeded.

        Delegates entirely to _LimitChecker.check_limits().  Returns None when
        all limits are within bounds.
        """
        return self._limits.check_limits(
            budget_backend=self._budget_backend,
            emit_fn=self._emit_chain_event_cb,
        )

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

    def _propagate_child_cost(
        self, cost_usd: float, _visited: frozenset[int] | None = None
    ) -> None:
        """Receive cost from a child context and accumulate it here.

        If accumulated cost exceeds ceiling, marks context as aborted and
        signals the CancellationToken. Propagates further if this context
        also has a parent.

        Args:
            cost_usd: Cost amount (in USD) spent by the child context.
            _visited: Internal set of already-visited context IDs to prevent
                infinite recursion if a circular parent chain is accidentally
                constructed (defensive programming).
        """
        # Guard against circular parent chains (should never occur in production
        # but defensive check prevents RecursionError if chains are misconfigured).
        my_id = id(self)
        if _visited is None:
            _visited = frozenset()
        if my_id in _visited:
            logger.warning(
                "ExecutionContext._propagate_child_cost: circular parent chain detected "
                "at context %d; stopping propagation to prevent infinite recursion",
                my_id,
            )
            return
        _visited = _visited | {my_id}

        new_total = self._limits.add_cost_returning(cost_usd)
        if new_total >= self._config.max_cost_usd:
            detail = (
                f"child propagation pushed chain total "
                f"${new_total:.4f} >= "
                f"ceiling ${self._config.max_cost_usd:.4f}"
            )
            if self._limits.mark_aborted(detail):
                # mark_aborted() returns True only on first abort -- emit event and
                # cancel token exactly once.
                self._emit_chain_event("budget_exceeded_by_child", detail)
                self._cancellation_token.cancel()
        # Propagate further up if we have a parent (outside lock to avoid deadlock).
        if self._parent is not None:
            self._parent._propagate_child_cost(cost_usd, _visited)

    def create_child(
        self,
        agent_name: str,
        agent_names: list[str],
        allocator: "BudgetAllocator | None" = None,
        current_usage: dict[str, float] | None = None,
        max_steps: int | None = None,
        max_retries_total: int | None = None,
        timeout_ms: int = 0,
        pipeline: "ShieldPipeline | None" = None,
    ) -> "ExecutionContext":
        """Create a child context with a budget share determined by *allocator*.

        Runs the allocator against the current remaining budget and returns a
        child context whose cost ceiling is the share assigned to *agent_name*.

        If no allocator is provided, the remaining budget is divided equally
        among *agent_names* and *agent_name*'s share is used.

        Args:
            agent_name: Name of the child agent being created. Must be in
                *agent_names*.
            agent_names: Full list of agent names competing for budget. Used
                by the allocator to compute shares.
            allocator: Strategy for distributing budget. If None, uses
                FairShareAllocator (equal split).
            current_usage: Per-agent USD already spent. Passed to the
                allocator; agents absent from the map default to 0.
            max_steps: Child step limit. Defaults to parent's.
            max_retries_total: Child retry budget. Defaults to parent's.
            timeout_ms: Child timeout in milliseconds. 0 = no timeout.
            pipeline: Optional ShieldPipeline for child.

        Returns:
            A new ExecutionContext linked to this parent with an allocated
            budget ceiling.

        Raises:
            ValueError: If *agent_name* is not in *agent_names*.

        Example::

            allocator = WeightedAllocator({"planner": 2, "executor": 1})
            with parent_ctx.create_child(
                agent_name="planner",
                agent_names=["planner", "executor"],
                allocator=allocator,
            ) as child:
                child.wrap_llm_call(planner_fn)
        """
        if agent_name not in agent_names:
            raise ValueError(
                f"agent_name {agent_name!r} must be present in agent_names {agent_names!r}."
            )

        from veronica_core.containment.budget_allocator import (
            FairShareAllocator,
        )

        remaining = self._config.max_cost_usd - self._limits.cost_usd_accumulated

        effective_allocator: BudgetAllocator = (
            allocator if allocator is not None else FairShareAllocator()
        )
        usage = current_usage or {}
        result = effective_allocator.allocate(
            total_budget=max(0.0, remaining),
            agent_names=agent_names,
            current_usage=usage,
        )
        child_budget = result.allocations.get(agent_name, 0.0)

        child_cfg = ExecutionConfig(
            max_cost_usd=child_budget,
            max_steps=max_steps if max_steps is not None else self._config.max_steps,
            max_retries_total=(
                max_retries_total
                if max_retries_total is not None
                else self._config.max_retries_total
            ),
            timeout_ms=timeout_ms,
        )
        return ExecutionContext(
            config=child_cfg,
            pipeline=pipeline,
            parent=self,
            memory_governor=self._memory_governor,
            policy_view_holder=self._policy_view_holder,
        )

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
        remaining = self._config.max_cost_usd - self._limits.cost_usd_accumulated
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
        return ExecutionContext(
            config=child_cfg,
            pipeline=pipeline,
            parent=self,
            memory_governor=self._memory_governor,
            policy_view_holder=self._policy_view_holder,
        )

    def _emit_chain_event(self, stop_reason: str, detail: str) -> None:
        """Append a chain-level SafetyEvent for *stop_reason*.

        Thin wrapper delegating to _event_log.emit_chain_event().  May be
        called with or without self._lock held; _ChainEventLog is independently
        thread-safe.

        Enriches the event with active policy metadata when a PolicyViewHolder
        is configured (v3.3 audit wiring).

        Args:
            stop_reason: Key from _STOP_REASON_EVENT_TYPE.
            detail: Human-readable explanation.
        """
        policy_metadata = self._get_policy_audit_metadata()
        self._event_log.emit_chain_event(
            stop_reason, detail, self._metadata.request_id,
            policy_metadata=policy_metadata,
        )

    def _get_policy_audit_metadata(self) -> dict[str, Any] | None:
        """Extract policy audit metadata from the active PolicyViewHolder.

        Returns None if no holder is configured or no view is loaded.
        Never raises -- swallows errors to prevent audit enrichment from
        disrupting the containment control flow.
        """
        if self._policy_view_holder is None:
            return None
        try:
            current = self._policy_view_holder.current
            if current is None:
                return None
            return current.to_audit_dict()
        except Exception:  # noqa: BLE001
            logger.debug(
                "ExecutionContext: policy_view_holder.current.to_audit_dict() failed",
                exc_info=True,
            )
            return None

