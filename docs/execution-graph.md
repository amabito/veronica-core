# ExecutionGraph — Call-Node Graph for Agent Chains

## 1. Motivation

`ExecutionContext` enforces chain-level limits (cost ceiling, step limit, retry budget,
timeout) and records a flat list of `NodeRecord` objects. That flat list is sufficient
for counting and budget enforcement, but it loses structural information:

- Which LLM call spawned which tool call?
- How deep did the agent recurse?
- Which sub-tree was still running when the chain halted?

`ExecutionGraph` adds a directed acyclic graph layer on top of `ExecutionContext`.
Every LLM call, tool call, and system operation becomes a **Node**. Nodes are linked
by parent-child relationships that reflect the actual call tree. Aggregate counters
(total cost, LLM call count, tool call count, retry count, max depth) are maintained
incrementally so callers never need to scan the full node list.

The graph is in-memory only. It is snapshotted on demand into a JSON-serializable dict
that can be logged, sent to a dashboard, or stored by the caller.

---

## 2. Definitions

| Term | Meaning |
|---|---|
| **Chain** | One agent run or request. Identified by `chain_id`. Contains exactly one root node and zero or more child nodes. |
| **Node** | One atomic operation within the chain: an LLM call, a tool invocation, or a system operation (e.g., the chain root itself). Identified by a unique `node_id`. |
| **Edge** | Implicit directed link from a parent node to a child node. Encoded as `child.parent_id = parent.node_id`. There are no explicit edge objects. |
| **Snapshot** | An immutable, deep-copied, JSON-serializable view of the graph state at a point in time. Produced by `ExecutionGraph.snapshot()`. |
| **Root** | The single entry-point node of a chain. Created by `create_root()`. Has `parent_id=None` and `kind="system"`. Depth 0. |

---

## 3. Required Invariants

The following invariants hold for every `ExecutionGraph` instance:

1. **Unique node_id** — No two nodes in the graph share a `node_id`. IDs are
   generated from a monotonically increasing counter and are never reused.

2. **parent_id discipline** — Every non-root node has a `parent_id` that refers
   to an existing node in the same graph. The root node has `parent_id=None`.
   Cycles are structurally impossible because a parent must exist before a child
   can reference it.

3. **Lifecycle** — Status transitions follow a strict one-way path:
   ```
   created -> running -> success | fail | halt
   created -> fail | halt        (pre-running terminal, e.g., budget exceeded)
   ```
   Once a node reaches a terminal status (`success`, `fail`, `halt`), its status
   cannot change. All mark_* methods are idempotent on already-terminal nodes.

4. **Monotonic costs** — `aggregates.total_cost_usd` never decreases. Cost is added
   when a node transitions to `success` and is never removed.

5. **Thread safety** — All mutations are protected by a single `threading.RLock`.
   Concurrent calls to `begin_node`, `mark_running`, `mark_success`, `mark_failure`,
   `mark_halt`, and `snapshot` are safe without external synchronization.

---

## 4. Minimal Data Model

### Node Fields

| Field | Type | Description |
|---|---|---|
| `node_id` | `str` | Unique identifier. Format: `n` followed by 6 zero-padded digits (e.g., `n000001`). |
| `parent_id` | `Optional[str]` | `node_id` of the parent node. `None` for the root. |
| `kind` | `"llm" \| "tool" \| "system"` | Type of operation this node represents. |
| `name` | `str` | Human-readable label (e.g., `"plan_step"`, `"web_search"`, `"agent_run"`). |
| `start_ts_ms` | `int` | UTC epoch milliseconds when the node was created. |
| `end_ts_ms` | `Optional[int]` | UTC epoch milliseconds when the node reached a terminal status. `None` if still in progress. |
| `status` | `"created" \| "running" \| "success" \| "fail" \| "halt"` | Current lifecycle stage. |
| `model` | `Optional[str]` | Model identifier. Relevant for `kind="llm"` nodes. |
| `retries_used` | `int` | Number of retry attempts made before the terminal transition. |
| `cost_usd` | `float` | Actual USD cost charged for this node. `0.0` for non-billable nodes. |
| `tokens_in` | `Optional[int]` | Input token count. `None` if not applicable or not yet known. |
| `tokens_out` | `Optional[int]` | Output token count. `None` if not applicable or not yet known. |
| `stop_reason` | `Optional[str]` | Human-readable explanation for why the node was halted or failed. |
| `error_class` | `Optional[str]` | Exception class name or error category (e.g., `"TimeoutError"`). Set on `fail` transitions. |
| `metadata` | `dict[str, Any]` | Arbitrary caller-supplied key-value pairs. |

### Chain Aggregates

Maintained incrementally. Updated atomically each time a node reaches a terminal status.

| Field | Type | Description |
|---|---|---|
| `total_cost_usd` | `float` | Sum of `cost_usd` across all `success` nodes. |
| `total_llm_calls` | `int` | Count of nodes with `kind="llm"` that reached `success`. |
| `total_tool_calls` | `int` | Count of nodes with `kind="tool"` that reached `success`. |
| `total_retries` | `int` | Sum of `retries_used` across all nodes that reached any terminal status. |
| `max_depth` | `int` | Maximum depth reached in the call tree (root = 0). Updated when `begin_node` is called. |

---

## 5. API Surface

All public methods on `ExecutionGraph` are thread-safe.

```python
class ExecutionGraph:
    def __init__(self, chain_id: Optional[str] = None) -> None: ...
```

`chain_id` defaults to a random UUID if not supplied. Pass the same `chain_id` used
by the parent `ExecutionContext` to correlate graph snapshots with context snapshots.

---

### `create_root(name, metadata?) -> root_id`

Create the root node for the chain. Must be called exactly once per graph, before
any `begin_node` calls. Raises `RuntimeError` on a second call.

```python
root_id = graph.create_root(name="agent_run", metadata={"request_id": "req-001"})
```

The root node has `kind="system"`, `parent_id=None`, and depth `0`.

---

### `begin_node(parent_id, kind, name, model?, metadata?) -> node_id`

Create a new child node attached to `parent_id`. The new node starts with
`status="created"`. Raises `KeyError` if `parent_id` does not exist.

```python
plan_id = graph.begin_node(
    parent_id=root_id,
    kind="llm",
    name="plan_step",
    model="claude-sonnet-4-6",
)
```

Depth of the new node is automatically `parent_depth + 1`. `max_depth` in the
aggregates is updated if the new depth exceeds the current maximum.

---

### `mark_running(node_id)`

Transition the node from `"created"` to `"running"`. Call this immediately before
dispatching the underlying operation. Idempotent on already-running or terminal nodes.

```python
graph.mark_running(plan_id)
```

---

### `mark_success(node_id, cost_usd, tokens_in?, tokens_out?)`

Transition the node to `"success"` and update aggregates. Idempotent on already-terminal
nodes (aggregates are NOT double-counted on repeat calls).

```python
graph.mark_success(plan_id, cost_usd=0.0042, tokens_in=120, tokens_out=80)
```

---

### `mark_failure(node_id, error_class, stop_reason?)`

Transition the node to `"fail"`. Safe to call before `mark_running`. Idempotent
on already-terminal nodes.

```python
graph.mark_failure(plan_id, error_class="RateLimitError", stop_reason="429 from provider")
```

---

### `mark_halt(node_id, stop_reason?)`

Transition the node to `"halt"` (policy-driven stop). Use this when a chain-level
policy (cost ceiling, circuit breaker, timeout) prevents the operation from
completing, rather than an application error. Idempotent on already-terminal nodes.

```python
graph.mark_halt(plan_id, stop_reason="cost ceiling exceeded")
```

---

### `snapshot() -> dict`

Return an immutable, JSON-serializable dict of the full graph state. Safe to call
at any time including while other threads are mutating the graph (snapshot is taken
under the lock). All mutable structures are deep-copied.

```python
snap = graph.snapshot()
# snap["chain_id"]         -> str
# snap["root_id"]          -> str | None
# snap["nodes"]            -> dict[node_id, node_fields_dict]
# snap["aggregates"]       -> dict with totals and max_depth
# snap["snapshot_ts_ms"]   -> int (epoch ms when snapshot was taken)
```

---

### `increment_retries(node_id)` (internal helper, callable by ExecutionContext)

Increment the `retries_used` counter on a node. Call once per retry attempt.
The count is folded into `aggregates.total_retries` when the node reaches a
terminal status.

---

## 6. How ExecutionContext Should Use It

`ExecutionGraph` is designed to be an optional companion to `ExecutionContext`.
The context enforces limits; the graph records structure.

Recommended integration pattern:

```python
from veronica_core.containment.execution_graph import ExecutionGraph

class ExecutionContext:
    def __init__(self, config, pipeline=None, metadata=None, ...):
        ...
        self._graph = ExecutionGraph(chain_id=self._metadata.chain_id)
        self._root_id = self._graph.create_root(
            name="chain",
            metadata={"request_id": self._metadata.request_id},
        )

    def _wrap(self, fn, kind, options):
        opts = options or WrapOptions()

        # Graph: begin node, link to last node or root.
        with self._lock:
            parent_id = self._nodes[-1].node_id if self._nodes else self._root_id
        graph_node_id = self._graph.begin_node(
            parent_id=parent_id,
            kind=kind,
            name=opts.operation_name or kind,
            model=self._metadata.model,
        )

        halt_reason = self._check_limits()
        if halt_reason is not None:
            self._graph.mark_halt(graph_node_id, stop_reason=halt_reason)
            return Decision.HALT

        self._graph.mark_running(graph_node_id)

        try:
            fn()
        except BaseException as exc:
            self._graph.mark_failure(graph_node_id, error_class=type(exc).__name__)
            return Decision.RETRY

        self._graph.mark_success(graph_node_id, cost_usd=opts.cost_estimate_hint)
        return Decision.ALLOW
```

The `ExecutionContext.get_snapshot()` method can include the graph snapshot:

```python
def get_snapshot(self):
    snap = self._graph.snapshot()
    return ContextSnapshot(
        ...,
        graph=snap,  # or store separately
    )
```

---

## 7. Example Snapshots

### Normal Chain (3 nodes, all success)

```json
{
  "chain_id": "chain-abc-123",
  "root_id": "n000001",
  "nodes": {
    "n000001": {
      "node_id": "n000001",
      "parent_id": null,
      "kind": "system",
      "name": "agent_run",
      "start_ts_ms": 1740000000000,
      "end_ts_ms": null,
      "status": "running",
      "model": null,
      "retries_used": 0,
      "cost_usd": 0.0,
      "tokens_in": null,
      "tokens_out": null,
      "stop_reason": null,
      "error_class": null,
      "metadata": {"request_id": "req-001"}
    },
    "n000002": {
      "node_id": "n000002",
      "parent_id": "n000001",
      "kind": "llm",
      "name": "plan_step",
      "start_ts_ms": 1740000000050,
      "end_ts_ms": 1740000001200,
      "status": "success",
      "model": "claude-sonnet-4-6",
      "retries_used": 0,
      "cost_usd": 0.0042,
      "tokens_in": 120,
      "tokens_out": 80,
      "stop_reason": null,
      "error_class": null,
      "metadata": {}
    },
    "n000003": {
      "node_id": "n000003",
      "parent_id": "n000002",
      "kind": "tool",
      "name": "web_search",
      "start_ts_ms": 1740000001250,
      "end_ts_ms": 1740000002100,
      "status": "success",
      "model": null,
      "retries_used": 0,
      "cost_usd": 0.0,
      "tokens_in": null,
      "tokens_out": null,
      "stop_reason": null,
      "error_class": null,
      "metadata": {"query": "VERONICA containment"}
    }
  },
  "aggregates": {
    "total_cost_usd": 0.0042,
    "total_llm_calls": 1,
    "total_tool_calls": 1,
    "total_retries": 0,
    "total_tokens_out": 820,
    "max_depth": 2
  },
  "snapshot_ts_ms": 1740000002200
}
```

### Halted Chain (cost ceiling exceeded mid-chain)

```json
{
  "chain_id": "chain-xyz-456",
  "root_id": "n000001",
  "nodes": {
    "n000001": {
      "node_id": "n000001",
      "parent_id": null,
      "kind": "system",
      "name": "agent_run",
      "start_ts_ms": 1740000010000,
      "end_ts_ms": null,
      "status": "running",
      "model": null,
      "retries_used": 0,
      "cost_usd": 0.0,
      "tokens_in": null,
      "tokens_out": null,
      "stop_reason": null,
      "error_class": null,
      "metadata": {}
    },
    "n000002": {
      "node_id": "n000002",
      "parent_id": "n000001",
      "kind": "llm",
      "name": "step_1",
      "start_ts_ms": 1740000010100,
      "end_ts_ms": 1740000011500,
      "status": "success",
      "model": "claude-sonnet-4-6",
      "retries_used": 0,
      "cost_usd": 0.95,
      "tokens_in": 5000,
      "tokens_out": 3000,
      "stop_reason": null,
      "error_class": null,
      "metadata": {}
    },
    "n000003": {
      "node_id": "n000003",
      "parent_id": "n000001",
      "kind": "llm",
      "name": "step_2",
      "start_ts_ms": 1740000011600,
      "end_ts_ms": 1740000011601,
      "status": "halt",
      "model": "claude-sonnet-4-6",
      "retries_used": 0,
      "cost_usd": 0.0,
      "tokens_in": null,
      "tokens_out": null,
      "stop_reason": "cost ceiling exceeded",
      "error_class": null,
      "metadata": {}
    }
  },
  "aggregates": {
    "total_cost_usd": 0.95,
    "total_llm_calls": 1,
    "total_tool_calls": 0,
    "total_retries": 0,
    "total_tokens_out": 3000,
    "max_depth": 1
  },
  "snapshot_ts_ms": 1740000011650
}
```

---

## 8. CHANGE SUMMARY

### What Changed

- **New file**: `src/veronica_core/containment/execution_graph.py`
  - `Node` dataclass with all required fields (14 fields including `metadata`).
  - `ExecutionGraph` class with thread-safe mutation via `threading.RLock`.
  - Monotonic node IDs with `"n"` prefix and 6-digit zero-padding.
  - Root node created via `create_root()` with `kind="system"`, `parent_id=None`, depth 0.
  - Incremental depth tracking: depth stored per node at `begin_node()` time.
  - Aggregate counters updated atomically on each terminal status transition.
  - `snapshot()` returns a deep-copied, JSON-serializable dict.
  - All `mark_*` methods are idempotent on already-terminal nodes.
  - `mark_failure` and `mark_halt` callable before `mark_running`.

- **New file**: `docs/execution-graph.md` — this document.

### Why It Matters

`ExecutionContext` answers "did this chain exceed its budget?" `ExecutionGraph`
answers "which specific call exceeded the budget, and what called it?" The graph
adds structural accountability that the flat `NodeRecord` list in `ContextSnapshot`
cannot provide.

### What Did NOT Change

- `ExecutionContext` is not modified by this task. Mark-2 handles the wiring.
- `__init__.py` is not updated. Mark-2 handles the export.
- `ShieldPipeline`, `NodeRecord`, `ContextSnapshot`, and all existing public APIs
  are unchanged.
- No external dependencies are added. `execution_graph.py` uses only the Python
  standard library (`copy`, `threading`, `time`, `uuid`, `dataclasses`, `typing`).
