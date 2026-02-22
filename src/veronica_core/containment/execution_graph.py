"""ExecutionGraph — directed acyclic graph of one agent chain's call nodes.

Tracks the parent-child relationships, timing, cost, and token counts for
every LLM and tool call made within a single chain (agent run or request).
Designed to be constructed incrementally as work proceeds, then snapshotted
for inspection or persistence.

Usage::

    graph = ExecutionGraph(chain_id="chain-abc-123")

    root_id = graph.create_root(name="agent_run")
    plan_id = graph.begin_node(parent_id=root_id, kind="llm", name="plan_step")
    graph.mark_running(plan_id)
    graph.mark_success(plan_id, cost_usd=0.0042, tokens_in=120, tokens_out=80)

    tool_id = graph.begin_node(parent_id=plan_id, kind="tool", name="web_search")
    graph.mark_running(tool_id)
    graph.mark_success(tool_id, cost_usd=0.0)

    snap = graph.snapshot()
"""

# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------
# Initial implementation:
#   - Node dataclass with all required fields
#   - Thread-safe via single RLock
#   - Monotonic node IDs with "n" prefix (n000001, n000002, ...)
#   - Incremental depth tracking
#   - Aggregate counters updated atomically on status transitions
#   - snapshot() returns deep-copied JSON-serializable dict
# ---------------------------------------------------------------------------

from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


__all__ = ["ExecutionGraph", "Node", "NodeSignature"]

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# NodeSignature identifies an operation kind by its (kind, name) pair.
# Used by the divergence-detection heuristic to track repeated patterns.
NodeSignature = tuple[str, str]


# ---------------------------------------------------------------------------
# Node dataclass
# ---------------------------------------------------------------------------

NodeKind = Literal["llm", "tool", "system"]
NodeStatus = Literal["created", "running", "success", "fail", "halt"]


@dataclass
class Node:
    """One node in the execution graph.

    A node represents a single LLM call, tool call, or system operation.
    Status transitions follow a strict lifecycle:

        created -> running -> success | fail | halt
        created -> fail | halt  (skip running if failed before dispatch)
    """

    node_id: str
    parent_id: Optional[str]
    kind: NodeKind
    name: str
    start_ts_ms: int
    end_ts_ms: Optional[int]
    status: NodeStatus
    model: Optional[str]
    retries_used: int
    cost_usd: float
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    stop_reason: Optional[str]
    error_class: Optional[str]
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# ExecutionGraph
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES: frozenset[NodeStatus] = frozenset({"success", "fail", "halt"})


class ExecutionGraph:
    """Directed acyclic graph tracking every node in one agent chain.

    Thread-safe: all mutations are protected by a single RLock so that
    multiple threads can call begin_node / mark_* concurrently without
    corrupting the graph or aggregate counters.

    Node IDs are monotonically increasing strings of the form "n000001",
    "n000002", etc. They are unique within this graph instance.

    Args:
        chain_id: Identifier for the chain this graph belongs to. If omitted,
            a random UUID is generated.
    """

    def __init__(self, chain_id: Optional[str] = None) -> None:
        self._chain_id: str = chain_id or str(uuid.uuid4())
        self._lock = threading.RLock()

        # Node storage and ID counter.
        self._nodes: dict[str, Node] = {}
        self._counter: int = 0

        # Root node (set by create_root).
        self._root_id: Optional[str] = None

        # Depth tracking: node_id -> depth (root = 0).
        self._depth: dict[str, int] = {}

        # Aggregate counters (updated atomically on terminal transitions).
        self._total_cost_usd: float = 0.0
        self._total_llm_calls: int = 0
        self._total_tool_calls: int = 0
        self._total_retries: int = 0
        self._max_depth: int = 0

        # Divergence detection state.
        # _sig_window: ring buffer of the last _K node signatures seen in
        # mark_running order.  Oldest entry is popped from the front when the
        # window exceeds _K entries.
        self._sig_window: list[NodeSignature] = []
        self._K: int = 8
        # Consecutive-repeat thresholds per kind.  "system" is set to 999 so
        # that chain-management nodes never trigger the heuristic.
        self._diverge_thresholds: dict[str, int] = {
            "tool": 3,
            "llm": 5,
            "system": 999,
        }
        # Frequency-based thresholds: fires when a signature appears >= N times
        # anywhere in the _K window, regardless of consecutiveness.  This catches
        # alternating patterns (A,B,A,B,...) that evade the consecutive check.
        self._freq_thresholds: dict[str, int] = {
            "tool": 5,
            "llm": 7,
            "system": 999,
        }
        # Deduplication: tracks (sig, mode) pairs so that consecutive and
        # frequency detections are independent, but each mode fires at most
        # once per signature per chain.
        # mode is "consecutive" or "frequency".
        self._emitted_divergences: set[tuple[NodeSignature, str]] = set()
        # Buffer of pending divergence event dicts.  Drained by
        # drain_divergence_events().
        self._pending_divergence_events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_root(
        self,
        name: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        """Create the root node for this chain.

        The root node has kind="system" and parent_id=None. Only one root
        node can exist per graph. Calling create_root a second time raises
        RuntimeError.

        Args:
            name: Human-readable name for the root operation (e.g., "agent_run").
            metadata: Optional arbitrary key-value pairs.

        Returns:
            node_id of the created root node.

        Raises:
            RuntimeError: If a root node has already been created.
        """
        with self._lock:
            if self._root_id is not None:
                raise RuntimeError(
                    f"Root node already exists: {self._root_id}. "
                    "ExecutionGraph supports exactly one root per chain."
                )
            node_id = self._next_id()
            now_ms = _now_ms()
            node = Node(
                node_id=node_id,
                parent_id=None,
                kind="system",
                name=name,
                start_ts_ms=now_ms,
                end_ts_ms=None,
                status="created",
                model=None,
                retries_used=0,
                cost_usd=0.0,
                tokens_in=None,
                tokens_out=None,
                stop_reason=None,
                error_class=None,
                metadata=dict(metadata) if metadata else {},
            )
            self._nodes[node_id] = node
            self._root_id = node_id
            self._depth[node_id] = 0
            return node_id

    def begin_node(
        self,
        parent_id: str,
        kind: NodeKind,
        name: str,
        model: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        """Create a new child node attached to *parent_id*.

        The node is created with status="created". Call mark_running() once
        the underlying operation starts, then mark_success / mark_failure /
        mark_halt when it completes.

        Args:
            parent_id: node_id of the parent node. Must already exist.
            kind: "llm", "tool", or "system".
            name: Human-readable label (e.g., "plan_step", "web_search").
            model: Model identifier, relevant for LLM nodes.
            metadata: Optional arbitrary key-value pairs.

        Returns:
            node_id of the new node.

        Raises:
            KeyError: If *parent_id* does not exist in the graph.
        """
        with self._lock:
            if parent_id not in self._nodes:
                raise KeyError(f"Parent node not found: {parent_id!r}")
            parent_depth = self._depth[parent_id]
            node_id = self._next_id()
            now_ms = _now_ms()
            node = Node(
                node_id=node_id,
                parent_id=parent_id,
                kind=kind,
                name=name,
                start_ts_ms=now_ms,
                end_ts_ms=None,
                status="created",
                model=model,
                retries_used=0,
                cost_usd=0.0,
                tokens_in=None,
                tokens_out=None,
                stop_reason=None,
                error_class=None,
                metadata=dict(metadata) if metadata else {},
            )
            self._nodes[node_id] = node
            node_depth = parent_depth + 1
            self._depth[node_id] = node_depth
            if node_depth > self._max_depth:
                self._max_depth = node_depth
            return node_id

    def mark_running(self, node_id: str) -> None:
        """Transition *node_id* from "created" to "running".

        Idempotent: calling mark_running on an already-running node is a
        no-op. Calling it on a terminal node is also a no-op (the node has
        already completed; no rollback occurs).

        Divergence detection: each call to mark_running (whether or not the
        status actually changes) records the node's (kind, name) signature in
        the ring buffer and may produce a divergence_suspected event.  The
        event is staged in self._pending_divergence_events; callers should
        drain it via drain_divergence_events() after calling mark_running.

        Args:
            node_id: Identifier of the node to update.

        Raises:
            KeyError: If *node_id* does not exist.
        """
        with self._lock:
            node = self._get_node(node_id)
            if node.status == "created":
                node.status = "running"
            sig: NodeSignature = (node.kind, node.name)
            event = self._update_sig_window(sig)
            if event is not None:
                self._pending_divergence_events.append(event)

    def mark_success(
        self,
        node_id: str,
        cost_usd: float,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
    ) -> None:
        """Transition *node_id* to "success" and update aggregates.

        Idempotent: if the node is already in a terminal status, the call
        is ignored and aggregates are NOT double-counted.

        Args:
            node_id: Identifier of the node to update.
            cost_usd: Actual USD cost charged for this operation.
            tokens_in: Input token count (optional).
            tokens_out: Output token count (optional).

        Raises:
            KeyError: If *node_id* does not exist.
        """
        with self._lock:
            node = self._get_node(node_id)
            if node.status in _TERMINAL_STATUSES:
                return
            sig: NodeSignature = (node.kind, node.name)
            event = self._update_sig_window(sig)
            if event is not None:
                self._pending_divergence_events.append(event)
            node.status = "success"
            node.end_ts_ms = _now_ms()
            node.cost_usd = cost_usd
            node.tokens_in = tokens_in
            node.tokens_out = tokens_out
            # Update aggregates.
            self._total_cost_usd += cost_usd
            self._total_retries += node.retries_used
            if node.kind == "llm":
                self._total_llm_calls += 1
            elif node.kind == "tool":
                self._total_tool_calls += 1

    def mark_failure(
        self,
        node_id: str,
        error_class: str,
        stop_reason: Optional[str] = None,
    ) -> None:
        """Transition *node_id* to "fail".

        Can be called even before mark_running (status transitions directly
        from "created" to "fail"). Idempotent on already-terminal nodes.

        A failed call still represents an attempted (and dispatched) operation,
        so llm/tool counters are incremented exactly as they are for success.
        This preserves the amplification semantics: a failed LLM call still
        consumed a call slot and should be reflected in llm_calls_per_root.

        Args:
            node_id: Identifier of the node to update.
            error_class: Exception class name or error category (e.g.,
                "TimeoutError", "RateLimitError").
            stop_reason: Optional human-readable explanation.

        Raises:
            KeyError: If *node_id* does not exist.
        """
        with self._lock:
            node = self._get_node(node_id)
            if node.status in _TERMINAL_STATUSES:
                return
            node.status = "fail"
            node.end_ts_ms = _now_ms()
            node.error_class = error_class
            node.stop_reason = stop_reason
            self._total_retries += node.retries_used
            if node.kind == "llm":
                self._total_llm_calls += 1
            elif node.kind == "tool":
                self._total_tool_calls += 1

    def mark_halt(
        self,
        node_id: str,
        stop_reason: Optional[str] = None,
    ) -> None:
        """Transition *node_id* to "halt" (policy-driven stop).

        Use "halt" to indicate a policy decision stopped this node (e.g.,
        cost ceiling exceeded, circuit breaker open) rather than an error.
        Can be called before mark_running. Idempotent on terminal nodes.

        A halted call represents attempted amplification: the policy fired
        because the call was about to be (or was being) dispatched. Both
        llm/tool counters are incremented so that llm_calls_per_root and
        tool_calls_per_root accurately reflect the containment pressure that
        caused the halt.

        Args:
            node_id: Identifier of the node to update.
            stop_reason: Optional human-readable explanation.

        Raises:
            KeyError: If *node_id* does not exist.
        """
        with self._lock:
            node = self._get_node(node_id)
            if node.status in _TERMINAL_STATUSES:
                return
            node.status = "halt"
            node.end_ts_ms = _now_ms()
            node.stop_reason = stop_reason
            self._total_retries += node.retries_used
            if node.kind == "llm":
                self._total_llm_calls += 1
            elif node.kind == "tool":
                self._total_tool_calls += 1

    def increment_retries(self, node_id: str) -> None:
        """Increment the retry counter for *node_id* by one.

        Call this each time a retry attempt is made before the final
        terminal transition. The count is included in aggregates when
        the node reaches a terminal state.

        Args:
            node_id: Identifier of the node to update.

        Raises:
            KeyError: If *node_id* does not exist.
        """
        with self._lock:
            node = self._get_node(node_id)
            node.retries_used += 1

    def drain_divergence_events(self) -> list[dict[str, Any]]:
        """Return and clear all pending divergence events.

        Should be called by ExecutionContext immediately after mark_running
        returns so that divergence_suspected SafetyEvents can be forwarded
        to the pipeline without delay.

        Thread-safe: runs under the existing RLock.

        Returns:
            List of event payload dicts (may be empty).  Each dict contains:
            - "event_type": "divergence_suspected"
            - "severity": "warn"
            - "signature": [kind, name]  (JSON-serializable list)
            - "repeat_count": int
            - "chain_id": str
        """
        with self._lock:
            events = self._pending_divergence_events
            self._pending_divergence_events = []
            return events

    def snapshot(self) -> dict[str, Any]:
        """Return an immutable, JSON-serializable snapshot of the graph.

        The returned dict contains:
        - "chain_id": str
        - "root_id": str or None
        - "nodes": dict mapping node_id to a dict of all node fields
        - "aggregates": dict with total_cost_usd, total_llm_calls,
          total_tool_calls, total_retries, max_depth,
          llm_calls_per_root, tool_calls_per_root, retries_per_root
        - "snapshot_ts_ms": int (current epoch milliseconds)

        Amplification fields (llm_calls_per_root, tool_calls_per_root,
        retries_per_root) are computed as total / root_count where
        root_count is always 1 for a single-chain graph. They are exposed
        as floats to preserve the division semantics for future multi-root
        aggregation without a breaking API change.

        Halt and fail nodes count toward llm/tool totals because they
        represent attempted (dispatched) calls; CREATED nodes do not count
        because they were never dispatched. The root system node is not
        counted toward llm or tool totals regardless of its status.

        All mutable structures are deep-copied; the caller may store or
        mutate the result without affecting the live graph.

        Returns:
            JSON-serializable dict describing the full graph state.
        """
        with self._lock:
            nodes_dict: dict[str, Any] = {}
            for nid, node in self._nodes.items():
                nodes_dict[nid] = {
                    "node_id": node.node_id,
                    "parent_id": node.parent_id,
                    "kind": node.kind,
                    "name": node.name,
                    "start_ts_ms": node.start_ts_ms,
                    "end_ts_ms": node.end_ts_ms,
                    "status": node.status,
                    "model": node.model,
                    "retries_used": node.retries_used,
                    "cost_usd": node.cost_usd,
                    "tokens_in": node.tokens_in,
                    "tokens_out": node.tokens_out,
                    "stop_reason": node.stop_reason,
                    "error_class": node.error_class,
                    "metadata": copy.deepcopy(node.metadata),
                }
            # root_count is always 1; expressed as a float so the division
            # remains meaningful if multi-root support is added later.
            root_count: float = 1.0
            return {
                "chain_id": self._chain_id,
                "root_id": self._root_id,
                "nodes": nodes_dict,
                "aggregates": {
                    "total_cost_usd": self._total_cost_usd,
                    "total_llm_calls": self._total_llm_calls,
                    "total_tool_calls": self._total_tool_calls,
                    "total_retries": self._total_retries,
                    "max_depth": self._max_depth,
                    "llm_calls_per_root": self._total_llm_calls / root_count,
                    "tool_calls_per_root": self._total_tool_calls / root_count,
                    "retries_per_root": self._total_retries / root_count,
                    # Count of unique (signature, mode) pairs for which a
                    # divergence_suspected event has been emitted.  Counts
                    # consecutive and frequency detections separately.
                    # Ephemeral pending events are NOT included.
                    "divergence_emitted_count": len(self._emitted_divergences),
                },
                "snapshot_ts_ms": _now_ms(),
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_sig_window(self, sig: NodeSignature) -> Optional[dict[str, Any]]:
        """Update the ring buffer and check for divergence via two strategies.

        Strategy 1 (consecutive): counts trailing repeats of *sig* and fires
        when they reach the kind-specific threshold.

        Strategy 2 (frequency): counts total occurrences of *sig* in the _K
        window regardless of position and fires when they reach the
        frequency-specific threshold.  This catches alternating patterns such
        as A,B,A,B,... that evade the consecutive check.

        Each (sig, mode) pair fires at most once per chain (deduplication).
        The first matching strategy wins for a given call; both may fire in
        different calls.

        Must be called with self._lock held.

        Args:
            sig: (kind, name) tuple identifying the operation.

        Returns:
            Event payload dict if divergence should be emitted, else None.
            Only the first matching check is returned per invocation to keep
            the pending-events list atomic per call.
        """
        self._sig_window.append(sig)
        if len(self._sig_window) > self._K:
            self._sig_window.pop(0)

        # --- Strategy 1: consecutive trailing repeats ---
        consecutive = 0
        for entry in reversed(self._sig_window):
            if entry == sig:
                consecutive += 1
            else:
                break

        consec_threshold = self._diverge_thresholds.get(sig[0], 999)
        consec_key = (sig, "consecutive")
        if consecutive >= consec_threshold and consec_key not in self._emitted_divergences:
            self._emitted_divergences.add(consec_key)
            return {
                "event_type": "divergence_suspected",
                "severity": "warn",
                "detection_mode": "consecutive",
                "signature": list(sig),
                "repeat_count": consecutive,
                "chain_id": self._chain_id,
            }

        # --- Strategy 2: frequency within window (non-consecutive pattern) ---
        # Only fires when the consecutive check did NOT already fire (or was
        # already deduped), specifically to catch alternating patterns like
        # A,B,A,B,... where consecutive count stays below threshold.
        # Condition: consecutive < consec_threshold (not a consecutive burst)
        # but total frequency in window >= freq_threshold.
        if consecutive < consec_threshold:
            freq_count = sum(1 for entry in self._sig_window if entry == sig)
            freq_threshold = self._freq_thresholds.get(sig[0], 999)
            freq_key = (sig, "frequency")
            if freq_count >= freq_threshold and freq_key not in self._emitted_divergences:
                self._emitted_divergences.add(freq_key)
                return {
                    "event_type": "divergence_suspected",
                    "severity": "warn",
                    "detection_mode": "frequency",
                    "signature": list(sig),
                    "repeat_count": freq_count,
                    "chain_id": self._chain_id,
                }

        return None

    def _next_id(self) -> str:
        """Return the next monotonic node ID (e.g., "n000001").

        Must be called with self._lock held.
        """
        self._counter += 1
        return f"n{self._counter:06d}"

    def _get_node(self, node_id: str) -> Node:
        """Return the Node for *node_id*.

        Must be called with self._lock held.

        Raises:
            KeyError: If *node_id* is not found.
        """
        try:
            return self._nodes[node_id]
        except KeyError:
            raise KeyError(f"Node not found: {node_id!r}") from None

    def _compute_depth(self, node_id: str) -> int:
        """Return the depth of *node_id* (root = 0).

        Walks the parent chain iteratively. Must be called with self._lock
        held. Returns -1 if *node_id* is not in the graph.
        """
        depth = 0
        current = node_id
        while current is not None:
            node = self._nodes.get(current)
            if node is None:
                return -1
            current = node.parent_id
            if current is not None:
                depth += 1
        return depth


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """Return the current UTC time as integer milliseconds since the epoch."""
    return int(time.time() * 1000)
