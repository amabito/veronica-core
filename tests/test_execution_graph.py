"""Unit tests for ExecutionGraph and ExecutionContext graph integration.

Test cases:
1. test_node_lifecycle_happy_path       - created -> running -> success transitions
2. test_halt_creates_node_with_stop_reason - halt with stop_reason recorded
3. test_aggregates_monotonic            - cost/llm/tool aggregates correct
4. test_tool_calls_increment_total_tool_calls - tool vs llm counters
5. test_thread_safety_no_duplicate_ids  - concurrent begin_node uniqueness
6. test_snapshot_is_deep_copy           - snapshot isolation
7. test_context_graph_snapshot_exposed  - ExecutionContext integration
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.containment.execution_graph import ExecutionGraph


# ---------------------------------------------------------------------------
# Test 1: Node lifecycle happy path
# ---------------------------------------------------------------------------


def test_node_lifecycle_happy_path():
    """Status transitions: created -> running -> success, with cost/tokens."""
    graph = ExecutionGraph(chain_id="test-chain-1")
    root_id = graph.create_root("agent_run")

    node_id = graph.begin_node(parent_id=root_id, kind="llm", name="plan_step")

    snap_before = graph.snapshot()
    assert snap_before["nodes"][node_id]["status"] == "created"

    graph.mark_running(node_id)
    snap_running = graph.snapshot()
    assert snap_running["nodes"][node_id]["status"] == "running"

    graph.mark_success(node_id, cost_usd=0.0042, tokens_in=120, tokens_out=80)
    snap_done = graph.snapshot()
    node = snap_done["nodes"][node_id]
    assert node["status"] == "success"
    assert abs(node["cost_usd"] - 0.0042) < 1e-9
    assert node["tokens_in"] == 120
    assert node["tokens_out"] == 80
    assert node["end_ts_ms"] is not None


# ---------------------------------------------------------------------------
# Test 2: Halt creates node with stop_reason
# ---------------------------------------------------------------------------


def test_halt_creates_node_with_stop_reason():
    """mark_halt records stop_reason and sets status to halt."""
    graph = ExecutionGraph(chain_id="test-chain-2")
    root_id = graph.create_root("chain_root")

    node_id = graph.begin_node(parent_id=root_id, kind="llm", name="budget_check")
    graph.mark_halt(node_id, stop_reason="budget_exceeded")

    snap = graph.snapshot()
    node = snap["nodes"][node_id]
    assert node["status"] == "halt"
    assert node["stop_reason"] == "budget_exceeded"
    assert node["end_ts_ms"] is not None


# ---------------------------------------------------------------------------
# Test 3: Aggregates are monotonic across multiple nodes
# ---------------------------------------------------------------------------


def test_aggregates_monotonic():
    """3 sequential llm calls: total_cost_usd == 0.03, total_llm_calls == 3."""
    graph = ExecutionGraph(chain_id="test-chain-3")
    root_id = graph.create_root("chain_root")

    costs = [0.01, 0.01, 0.01]
    for i, cost in enumerate(costs):
        node_id = graph.begin_node(parent_id=root_id, kind="llm", name=f"step_{i}")
        graph.mark_running(node_id)
        graph.mark_success(node_id, cost_usd=cost)

    snap = graph.snapshot()
    agg = snap["aggregates"]
    assert abs(agg["total_cost_usd"] - 0.03) < 1e-9, f"Expected 0.03, got {agg['total_cost_usd']}"
    assert agg["total_llm_calls"] == 3
    assert agg["total_tool_calls"] == 0
    assert agg["total_retries"] >= 0


# ---------------------------------------------------------------------------
# Test 4: Tool calls increment total_tool_calls, not total_llm_calls
# ---------------------------------------------------------------------------


def test_tool_calls_increment_total_tool_calls():
    """Tool nodes counted in total_tool_calls, not total_llm_calls."""
    graph = ExecutionGraph(chain_id="test-chain-4")
    root_id = graph.create_root("chain_root")

    tool_id = graph.begin_node(parent_id=root_id, kind="tool", name="web_search")
    graph.mark_running(tool_id)
    graph.mark_success(tool_id, cost_usd=0.0)

    snap = graph.snapshot()
    agg = snap["aggregates"]
    assert agg["total_tool_calls"] == 1
    assert agg["total_llm_calls"] == 0


# ---------------------------------------------------------------------------
# Test 5: Thread safety - no duplicate node IDs under concurrency
# ---------------------------------------------------------------------------


def test_thread_safety_no_duplicate_ids():
    """100 concurrent begin_node calls must produce 100 unique node IDs."""
    graph = ExecutionGraph(chain_id="test-chain-5")
    root_id = graph.create_root("chain_root")

    node_ids: list[str] = []
    lock = threading.Lock()

    def create_node(_: int) -> None:
        nid = graph.begin_node(parent_id=root_id, kind="llm", name="concurrent")
        with lock:
            node_ids.append(nid)

    threads = [threading.Thread(target=create_node, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(node_ids) == 100, f"Expected 100 node IDs, got {len(node_ids)}"
    assert len(set(node_ids)) == 100, "Duplicate node IDs detected under concurrency"


# ---------------------------------------------------------------------------
# Test 6: snapshot() returns a deep copy (mutating result does not affect graph)
# ---------------------------------------------------------------------------


def test_snapshot_is_deep_copy():
    """Mutating a snapshot dict does not affect the live graph or later snapshots."""
    graph = ExecutionGraph(chain_id="test-chain-6")
    root_id = graph.create_root("chain_root")

    node_id = graph.begin_node(parent_id=root_id, kind="llm", name="my_step")
    graph.mark_success(node_id, cost_usd=0.05)

    snap1 = graph.snapshot()
    # Mutate the snapshot
    snap1["aggregates"]["total_cost_usd"] = 9999.0
    snap1["nodes"][node_id]["status"] = "mutated"

    snap2 = graph.snapshot()
    # Live graph must be unaffected
    assert abs(snap2["aggregates"]["total_cost_usd"] - 0.05) < 1e-9
    assert snap2["nodes"][node_id]["status"] == "success"


# ---------------------------------------------------------------------------
# Test 7: ExecutionContext exposes graph snapshot with correct aggregates
# ---------------------------------------------------------------------------


def test_context_graph_snapshot_exposed():
    """ctx.get_graph_snapshot() returns graph with root + 1 llm node."""
    config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
    ctx = ExecutionContext(config=config)

    ctx.wrap_llm_call(
        fn=lambda: None,
        options=WrapOptions(operation_name="my_llm_call", cost_estimate_hint=0.02),
    )

    graph_snap = ctx.get_graph_snapshot()
    nodes = graph_snap["nodes"]
    agg = graph_snap["aggregates"]

    # Should have root node (system) + 1 llm node
    assert len(nodes) == 2, f"Expected 2 nodes (root + llm), got {len(nodes)}"

    # Find root and llm nodes
    kinds = {n["kind"] for n in nodes.values()}
    assert "system" in kinds, "Root node with kind='system' expected"
    assert "llm" in kinds, "LLM node expected"

    assert agg["total_llm_calls"] == 1
    assert agg["total_tool_calls"] == 0
    assert abs(agg["total_cost_usd"] - 0.02) < 1e-9
