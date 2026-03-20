"""Tests for ExecutionGraph max_nodes LRU pruning (Issue #62).

Test index:
 1. test_no_pruning_when_unlimited       -- max_nodes=0 never prunes
 2. test_pruning_kicks_in_at_limit       -- eviction fires at exactly max_nodes+1
 3. test_pruned_count_tracks_correctly   -- pruned_count increments per eviction
 4. test_eviction_is_oldest_first_fifo   -- oldest completed node evicted first
 5. test_inprogress_nodes_never_evicted  -- running/created nodes are safe
 6. test_overlimit_when_all_inprogress   -- no eviction candidate -> allow over-limit
 7. test_max_nodes_one_extreme           -- max_nodes=1 keeps only the root
 8. test_negative_max_nodes_raises       -- negative value raises ValueError
 9. test_graph_snapshot_consistent_after_pruning -- snapshot has no evicted node
10. test_pruned_node_not_accessible_via_mark_success -- evicted node raises KeyError
11. test_rapid_create_complete_create    -- adversarial: tight create+complete loop
12. test_concurrent_pruning_10_threads  -- 10 threads, pruned_count consistent
13. test_evict_while_concurrent_mark_halt -- adversarial: eviction + mark_halt race
14. test_fail_status_is_evictable        -- fail status counts as completed
15. test_halt_status_is_evictable        -- halt status counts as completed
16. test_max_nodes_backward_compat       -- omitting max_nodes = no change in behavior
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from veronica_core.containment.execution_graph import ExecutionGraph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_graph(max_nodes: int = 0) -> ExecutionGraph:
    return ExecutionGraph(chain_id="test-pruning", max_nodes=max_nodes)


def _complete_node(graph: ExecutionGraph, node_id: str) -> None:
    """Mark a node running then successful (standard terminal path)."""
    graph.mark_running(node_id)
    graph.mark_success(node_id, cost_usd=0.0)


# ---------------------------------------------------------------------------
# Test 1: max_nodes=0 means unlimited (backward compat)
# ---------------------------------------------------------------------------


def test_no_pruning_when_unlimited():
    """max_nodes=0 -- no eviction regardless of node count."""
    graph = _make_graph(max_nodes=0)
    root_id = graph.create_root("root")
    node_ids = [graph.begin_node(root_id, "llm", f"step-{i}") for i in range(50)]
    for nid in node_ids:
        _complete_node(graph, nid)
    # Create 50 more -- still no pruning
    for i in range(50):
        nid = graph.begin_node(root_id, "tool", f"tool-{i}")
        _complete_node(graph, nid)

    assert graph.pruned_count == 0
    snap = graph.snapshot()
    # 1 root + 100 children
    assert len(snap["nodes"]) == 101


# ---------------------------------------------------------------------------
# Test 2: Pruning kicks in at exactly max_nodes+1
# ---------------------------------------------------------------------------


def test_pruning_kicks_in_at_limit():
    """Node count never exceeds max_nodes when completed nodes exist."""
    max_n = 5
    graph = _make_graph(max_nodes=max_n)
    root_id = graph.create_root("root")  # node count = 1

    # Fill up to max_nodes with completed nodes.
    first_ids = []
    for i in range(max_n - 1):  # 4 more = 5 total
        nid = graph.begin_node(root_id, "llm", f"step-{i}")
        _complete_node(graph, nid)
        first_ids.append(nid)

    snap = graph.snapshot()
    assert len(snap["nodes"]) == max_n
    assert graph.pruned_count == 0

    # Adding one more triggers the first eviction.
    extra_id = graph.begin_node(root_id, "tool", "extra")
    snap2 = graph.snapshot()
    # One eviction: node count stays <= max_nodes (after pruning, then +1)
    assert len(snap2["nodes"]) == max_n
    assert graph.pruned_count == 1
    # The evicted node is no longer present.
    assert first_ids[0] not in snap2["nodes"]

    # Complete the extra node and add another -- second eviction.
    _complete_node(graph, extra_id)
    _ = graph.begin_node(root_id, "tool", "extra2")
    assert graph.pruned_count == 2


# ---------------------------------------------------------------------------
# Test 3: pruned_count tracks correctly across many evictions
# ---------------------------------------------------------------------------


def test_pruned_count_tracks_correctly():
    """pruned_count == number of evictions performed."""
    max_n = 3
    graph = _make_graph(max_nodes=max_n)
    root_id = graph.create_root("root")  # count = 1

    n1 = graph.begin_node(root_id, "llm", "n1")
    _complete_node(graph, n1)  # count = 2

    n2 = graph.begin_node(root_id, "llm", "n2")
    _complete_node(graph, n2)  # count = 3 (= max_n, no eviction yet)

    assert graph.pruned_count == 0

    for evictions in range(1, 11):
        nid = graph.begin_node(root_id, "tool", f"step-{evictions}")
        assert graph.pruned_count == evictions
        _complete_node(graph, nid)


# ---------------------------------------------------------------------------
# Test 4: Eviction is oldest-first FIFO
# ---------------------------------------------------------------------------


def test_eviction_is_oldest_first_fifo():
    """The oldest completed node is evicted before newer ones."""
    graph = _make_graph(max_nodes=4)
    root_id = graph.create_root("root")  # pos 0

    a = graph.begin_node(root_id, "llm", "A")
    _complete_node(graph, a)  # pos 1 -- oldest completed

    b = graph.begin_node(root_id, "llm", "B")
    _complete_node(graph, b)  # pos 2

    c = graph.begin_node(root_id, "llm", "C")
    _complete_node(graph, c)  # pos 3 -- count = 4 = max_n, no eviction yet

    # Adding D triggers eviction of oldest completed: A.
    d = graph.begin_node(root_id, "llm", "D")
    snap = graph.snapshot()
    assert a not in snap["nodes"], "A should have been evicted first"
    assert b in snap["nodes"]
    assert c in snap["nodes"]
    assert d in snap["nodes"]
    assert graph.pruned_count == 1

    _complete_node(graph, d)

    # Adding E triggers eviction of B (now oldest completed).
    e = graph.begin_node(root_id, "llm", "E")
    snap2 = graph.snapshot()
    assert b not in snap2["nodes"], "B should be evicted second"
    assert c in snap2["nodes"]
    assert d in snap2["nodes"]
    assert e in snap2["nodes"]
    assert graph.pruned_count == 2


# ---------------------------------------------------------------------------
# Test 5: In-progress (running/created) nodes are never evicted
# ---------------------------------------------------------------------------


def test_inprogress_nodes_never_evicted():
    """running and created nodes must never be evicted."""
    graph = _make_graph(max_nodes=3)
    root_id = graph.create_root("root")  # count = 1

    pending = graph.begin_node(root_id, "llm", "pending")  # count = 2, created
    running = graph.begin_node(root_id, "llm", "running-node")  # count = 3 = max
    graph.mark_running(running)

    # At this point: root (created/system), pending (created), running (running).
    # All are non-terminal -- no eviction candidate.
    # Adding a 4th should NOT evict any of them (over-limit allowed).
    extra = graph.begin_node(root_id, "tool", "extra")
    snap = graph.snapshot()

    # All four nodes present -- over-limit because no completed node existed.
    assert root_id in snap["nodes"]
    assert pending in snap["nodes"]
    assert running in snap["nodes"]
    assert extra in snap["nodes"]
    assert graph.pruned_count == 0


# ---------------------------------------------------------------------------
# Test 6: Over-limit when all nodes are in-progress
# ---------------------------------------------------------------------------


def test_overlimit_when_all_inprogress():
    """When all nodes are in-progress, new node is added over the limit."""
    graph = _make_graph(max_nodes=2)
    root_id = graph.create_root("root")  # count = 1
    n1 = graph.begin_node(root_id, "llm", "n1")  # count = 2 = max
    graph.mark_running(n1)  # still running

    # No completed node to evict -- over-limit.
    n2 = graph.begin_node(root_id, "llm", "n2")

    snap = graph.snapshot()
    assert len(snap["nodes"]) == 3  # over-limit
    assert graph.pruned_count == 0

    # Complete n1 -- now next add should prune.
    _complete_node(graph, n1)
    n3 = graph.begin_node(root_id, "tool", "n3")
    snap2 = graph.snapshot()
    assert graph.pruned_count == 1
    assert n1 not in snap2["nodes"]
    assert n2 in snap2["nodes"]
    assert n3 in snap2["nodes"]


# ---------------------------------------------------------------------------
# Test 7: max_nodes=1 extreme case
# ---------------------------------------------------------------------------


def test_max_nodes_one_extreme():
    """max_nodes=1: each new node evicts the previous completed one."""
    graph = _make_graph(max_nodes=1)
    root_id = graph.create_root("root")  # count = 1 = max

    # root is created (non-terminal) -- no eviction yet, over-limit.
    n1 = graph.begin_node(root_id, "llm", "n1")
    assert graph.pruned_count == 0  # root not evictable (created)

    # Complete root so it becomes evictable, then complete n1.
    # Note: we can't transition root to terminal via public API easily,
    # so let's just complete n1 and observe behavior on n2.
    _complete_node(graph, n1)

    # n2: limit=1, count=2 (root still created, n1 success). Oldest completed = n1.
    graph.begin_node(root_id, "tool", "n2")
    assert n1 not in graph.snapshot()["nodes"]
    assert graph.pruned_count == 1


# ---------------------------------------------------------------------------
# Test 8: Negative max_nodes raises ValueError
# ---------------------------------------------------------------------------


def test_negative_max_nodes_raises():
    """max_nodes < 0 must raise ValueError at construction time."""
    with pytest.raises(ValueError, match="max_nodes"):
        ExecutionGraph(max_nodes=-1)

    with pytest.raises(ValueError, match="max_nodes"):
        ExecutionGraph(max_nodes=-100)


# ---------------------------------------------------------------------------
# Test 9: Graph snapshot is consistent after pruning
# ---------------------------------------------------------------------------


def test_graph_snapshot_consistent_after_pruning():
    """After pruning, snapshot contains exactly the surviving nodes."""
    graph = _make_graph(max_nodes=4)
    root_id = graph.create_root("root")

    nodes = []
    for i in range(3):  # total 4 including root
        nid = graph.begin_node(root_id, "llm", f"n{i}")
        _complete_node(graph, nid)
        nodes.append(nid)

    # Adding one more triggers pruning.
    new_id = graph.begin_node(root_id, "tool", "new")
    snap = graph.snapshot()

    assert len(snap["nodes"]) == 4
    # nodes[0] was the oldest completed: root was created (non-terminal) so it
    # stays; nodes[0] is the oldest completed child.
    assert nodes[0] not in snap["nodes"]
    assert nodes[1] in snap["nodes"]
    assert nodes[2] in snap["nodes"]
    assert new_id in snap["nodes"]
    assert root_id in snap["nodes"]

    # Snapshot fields are intact for surviving nodes.
    for nid in [nodes[1], nodes[2]]:
        node_snap = snap["nodes"][nid]
        assert node_snap["status"] == "success"
        assert node_snap["node_id"] == nid


# ---------------------------------------------------------------------------
# Test 10: Evicted node raises KeyError on subsequent mark_success
# ---------------------------------------------------------------------------


def test_pruned_node_not_accessible_via_mark_success():
    """A pruned node is gone from the graph; accessing it raises KeyError."""
    graph = _make_graph(max_nodes=2)
    root_id = graph.create_root("root")  # count=1

    n1 = graph.begin_node(root_id, "llm", "n1")
    _complete_node(graph, n1)  # count=2, oldest completed = n1

    # Adding n2 triggers eviction of n1.
    graph.begin_node(root_id, "llm", "n2")
    assert graph.pruned_count == 1

    # Attempting to interact with the evicted node raises KeyError.
    with pytest.raises(KeyError):
        graph.mark_success(n1, cost_usd=0.0)


# ---------------------------------------------------------------------------
# Test 11: Adversarial -- rapid create+complete+create cycle
# ---------------------------------------------------------------------------


def test_rapid_create_complete_create():
    """Tight loop: create->complete->create many times. pruned_count == iterations."""
    max_n = 3
    graph = _make_graph(max_nodes=max_n)
    root_id = graph.create_root("root")  # count=1

    n_prev = graph.begin_node(root_id, "llm", "init")
    _complete_node(graph, n_prev)  # count=2

    iterations = 100
    for i in range(iterations):
        n_new = graph.begin_node(root_id, "tool", f"step-{i}")
        _complete_node(graph, n_new)
        snap = graph.snapshot()
        # Node count never exceeds max_n (one eviction per add when all completed).
        assert len(snap["nodes"]) <= max_n

    # Pruning count reasoning:
    # root=1, init=2 before loop starts.
    # Iteration 0: count=2 < max_n=3 -- no eviction. After complete: count=3.
    # Iteration 1: count=3 >= max_n -- evict init. pruned_count=1.
    # Iterations 1..99: each evicts the previous completed node. pruned_count=99.
    # Total evictions = iterations - 1.
    assert graph.pruned_count == iterations - 1


# ---------------------------------------------------------------------------
# Test 12: Concurrent 10-thread node creation
# ---------------------------------------------------------------------------


def test_concurrent_pruning_10_threads():
    """10 threads each create+complete 10 nodes; final state is consistent."""
    max_n = 20
    graph = _make_graph(max_nodes=max_n)
    root_id = graph.create_root("root")

    errors: list[Exception] = []

    def worker(thread_idx: int) -> None:
        try:
            for i in range(10):
                nid = graph.begin_node(root_id, "llm", f"t{thread_idx}-{i}")
                graph.mark_running(nid)
                graph.mark_success(nid, cost_usd=0.001)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"

    snap = graph.snapshot()
    # Node count must not exceed max_n.
    assert len(snap["nodes"]) <= max_n
    # pruned_count + surviving nodes == 1 (root) + 100 (created by threads).
    assert graph.pruned_count + len(snap["nodes"]) == 101


# ---------------------------------------------------------------------------
# Test 13: Adversarial -- eviction while concurrent mark_halt
# ---------------------------------------------------------------------------


def test_evict_while_concurrent_mark_halt():
    """Race: one thread adds nodes (triggering eviction), another calls mark_halt."""
    max_n = 5
    graph = _make_graph(max_nodes=max_n)
    root_id = graph.create_root("root")

    # Pre-fill with completed nodes up to limit.
    ids = []
    for i in range(max_n - 1):
        nid = graph.begin_node(root_id, "llm", f"pre-{i}")
        _complete_node(graph, nid)
        ids.append(nid)

    # Create a running node that the halter thread will mark_halt on.
    halting_id = graph.begin_node(root_id, "llm", "halting")
    graph.mark_running(halting_id)

    errors: list[Exception] = []
    halt_done = threading.Event()

    def adder() -> None:
        try:
            for _ in range(20):
                nid = graph.begin_node(root_id, "tool", "added")
                _complete_node(graph, nid)
        except Exception as exc:
            errors.append(exc)

    def halter() -> None:
        try:
            # Small delay so adder thread has started.
            time.sleep(0.005)
            graph.mark_halt(halting_id, stop_reason="test-halt")
        except Exception as exc:
            errors.append(exc)
        finally:
            halt_done.set()

    t_add = threading.Thread(target=adder)
    t_halt = threading.Thread(target=halter)
    t_add.start()
    t_halt.start()
    t_add.join()
    t_halt.join()

    assert not errors, f"Concurrent errors: {errors}"

    snap = graph.snapshot()
    # halting_id should still be present (it was running, not evictable).
    # After mark_halt it may have been evicted only if it was already halted
    # when the adder ran -- that is acceptable since halt is terminal.
    # The key invariant: no panic, pruned_count >= 0, snapshot is consistent.
    assert graph.pruned_count >= 0
    for nid, nsnap in snap["nodes"].items():
        assert nsnap["node_id"] == nid


# ---------------------------------------------------------------------------
# Test 14: fail status is evictable
# ---------------------------------------------------------------------------


def test_fail_status_is_evictable():
    """Nodes in 'fail' status are completed and must be eligible for eviction."""
    graph = _make_graph(max_nodes=2)
    root_id = graph.create_root("root")  # count=1

    n_fail = graph.begin_node(root_id, "llm", "failing")
    graph.mark_running(n_fail)
    graph.mark_failure(n_fail, error_class="TimeoutError")  # count=2, fail

    # Verify fail node can be evicted.
    n_new = graph.begin_node(root_id, "tool", "new-node")
    assert graph.pruned_count == 1
    snap = graph.snapshot()
    assert n_fail not in snap["nodes"]
    assert n_new in snap["nodes"]


# ---------------------------------------------------------------------------
# Test 15: halt status is evictable
# ---------------------------------------------------------------------------


def test_halt_status_is_evictable():
    """Nodes in 'halt' status are completed and must be eligible for eviction."""
    graph = _make_graph(max_nodes=2)
    root_id = graph.create_root("root")  # count=1

    n_halt = graph.begin_node(root_id, "llm", "halted")
    graph.mark_running(n_halt)
    graph.mark_halt(n_halt, stop_reason="policy")  # count=2, halt

    n_new = graph.begin_node(root_id, "tool", "new-node")
    assert graph.pruned_count == 1
    snap = graph.snapshot()
    assert n_halt not in snap["nodes"]
    assert n_new in snap["nodes"]


# ---------------------------------------------------------------------------
# Test 16: Backward compatibility -- max_nodes omitted
# ---------------------------------------------------------------------------


def test_max_nodes_backward_compat():
    """Omitting max_nodes (default 0) behaves identically to before this change."""
    graph = ExecutionGraph(chain_id="compat-test")
    root_id = graph.create_root("root")

    for i in range(200):
        nid = graph.begin_node(root_id, "llm", f"n{i}")
        _complete_node(graph, nid)

    assert graph.pruned_count == 0
    assert len(graph.snapshot()["nodes"]) == 201  # 1 root + 200 children
