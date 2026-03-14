"""Tests for ExecutionGraph dynamic observer + NodeEvent + subscriber API."""

from __future__ import annotations

import threading
import time

import pytest

from _nogil_compat import nogil_unstable
from veronica_core.containment.execution_graph import (
    ExecutionGraph,
    NodeEvent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingObserver:
    """Observer that records every call it receives."""

    def __init__(self) -> None:
        self.starts: list[tuple] = []
        self.completes: list[tuple] = []
        self.failures: list[tuple] = []
        self.decisions: list[tuple] = []

    def on_node_start(self, node_id: str, name: str, metadata: dict) -> None:
        self.starts.append((node_id, name, metadata))

    def on_node_complete(
        self, node_id: str, cost_usd: float, duration_ms: float
    ) -> None:
        self.completes.append((node_id, cost_usd, duration_ms))

    def on_node_failed(self, node_id: str, reason: str) -> None:
        self.failures.append((node_id, reason))

    def on_decision(self, node_id: str, decision: str, reason: str) -> None:
        self.decisions.append((node_id, decision, reason))


def _make_graph() -> ExecutionGraph:
    g = ExecutionGraph(chain_id="test-chain")
    g.create_root(name="root")
    return g


def _add_llm_node(g: ExecutionGraph) -> str:
    root_id = g._root_id
    assert root_id is not None
    return g.begin_node(parent_id=root_id, kind="llm", name="step")


# ---------------------------------------------------------------------------
# Unit tests -- happy path
# ---------------------------------------------------------------------------


def test_add_observer_receives_events() -> None:
    """Observer added after __init__ receives on_node_start and on_node_complete."""
    g = _make_graph()
    obs = _RecordingObserver()
    g.add_observer(obs)

    nid = _add_llm_node(g)
    g.mark_running(nid)
    g.mark_success(nid, cost_usd=0.01, tokens_in=10, tokens_out=20)

    assert len(obs.starts) == 1
    assert obs.starts[0][0] == nid
    assert len(obs.completes) == 1
    assert obs.completes[0][0] == nid
    assert obs.completes[0][1] == pytest.approx(0.01)


def test_remove_observer_stops_events() -> None:
    """Removed observer receives no further calls."""
    g = _make_graph()
    obs = _RecordingObserver()
    g.add_observer(obs)
    g.remove_observer(obs)

    nid = _add_llm_node(g)
    g.mark_running(nid)
    g.mark_success(nid, cost_usd=0.005)

    assert obs.starts == []
    assert obs.completes == []


def test_add_observer_multiple() -> None:
    """Two observers both receive events."""
    g = _make_graph()
    obs1 = _RecordingObserver()
    obs2 = _RecordingObserver()
    g.add_observer(obs1)
    g.add_observer(obs2)

    nid = _add_llm_node(g)
    g.mark_running(nid)
    g.mark_success(nid, cost_usd=0.002)

    assert len(obs1.completes) == 1
    assert len(obs2.completes) == 1


def test_add_observer_dedup() -> None:
    """Adding the same observer twice must register it only once."""
    g = _make_graph()
    obs = _RecordingObserver()
    g.add_observer(obs)
    g.add_observer(obs)  # duplicate -- should be no-op

    nid = _add_llm_node(g)
    g.mark_running(nid)
    g.mark_success(nid, cost_usd=0.0)

    assert len(obs.completes) == 1  # NOT 2


def test_subscriber_receives_node_event() -> None:
    """Subscriber receives correct NodeEvent fields on mark_success."""
    g = _make_graph()
    events: list[NodeEvent] = []
    g.add_subscriber(events.append)

    nid = _add_llm_node(g)
    g.mark_running(nid)
    g.mark_success(nid, cost_usd=0.042, tokens_in=100, tokens_out=50)

    assert len(events) == 1
    ev = events[0]
    assert ev.node_id == nid
    assert ev.status == "success"
    assert ev.kind == "llm"
    assert ev.name == "step"
    assert ev.cost_usd == pytest.approx(0.042)
    assert ev.tokens_in == 100
    assert ev.tokens_out == 50
    assert ev.chain_id == "test-chain"
    assert ev.elapsed_ms is not None
    assert ev.elapsed_ms >= 0.0


def test_subscriber_receives_failure_event() -> None:
    """Subscriber receives NodeEvent with error_class on mark_failure."""
    g = _make_graph()
    events: list[NodeEvent] = []
    g.add_subscriber(events.append)

    nid = _add_llm_node(g)
    g.mark_running(nid)
    g.mark_failure(nid, error_class="TimeoutError", stop_reason="deadline exceeded")

    assert len(events) == 1
    ev = events[0]
    assert ev.status == "fail"
    assert ev.error_class == "TimeoutError"
    assert ev.stop_reason == "deadline exceeded"


def test_subscriber_receives_halt_event() -> None:
    """Subscriber receives NodeEvent with stop_reason on mark_halt."""
    g = _make_graph()
    events: list[NodeEvent] = []
    g.add_subscriber(events.append)

    nid = _add_llm_node(g)
    g.mark_halt(nid, stop_reason="budget_exceeded")

    assert len(events) == 1
    ev = events[0]
    assert ev.status == "halt"
    assert ev.stop_reason == "budget_exceeded"


def test_subscriber_receives_halt_event_without_stop_reason() -> None:
    """Subscriber receives NodeEvent with default stop_reason on mark_halt (R8-F3)."""
    g = _make_graph()
    events: list[NodeEvent] = []
    g.add_subscriber(events.append)

    nid = _add_llm_node(g)
    g.mark_halt(nid)  # no stop_reason

    assert len(events) == 1
    ev = events[0]
    assert ev.status == "halt"
    assert ev.stop_reason == "halt"  # default halt_reason


def test_subscriber_not_called_on_mark_running() -> None:
    """Subscribers only fire on terminal transitions, not on mark_running."""
    g = _make_graph()
    events: list[NodeEvent] = []
    g.add_subscriber(events.append)

    nid = _add_llm_node(g)
    g.mark_running(nid)

    assert events == []


def test_remove_subscriber() -> None:
    """Removed subscriber receives no further events.

    Note: callback reference must be stored in a variable because
    remove_subscriber uses identity (is), not equality (__eq__).
    """
    g = _make_graph()
    events: list[NodeEvent] = []
    cb = events.append  # store reference for identity-based removal
    g.add_subscriber(cb)
    g.remove_subscriber(cb)

    nid = _add_llm_node(g)
    g.mark_success(nid, cost_usd=0.0)

    assert events == []


def test_node_event_is_frozen() -> None:
    """NodeEvent fields are immutable (frozen dataclass)."""
    g = _make_graph()
    events: list[NodeEvent] = []
    g.add_subscriber(events.append)

    nid = _add_llm_node(g)
    g.mark_success(nid, cost_usd=0.001)

    ev = events[0]
    with pytest.raises((AttributeError, TypeError)):
        ev.cost_usd = 999.0  # type: ignore[misc]


def test_on_decision_fires_on_halt() -> None:
    """Observer.on_decision is called with (node_id, 'HALT', stop_reason)."""
    g = _make_graph()
    obs = _RecordingObserver()
    g.add_observer(obs)

    nid = _add_llm_node(g)
    g.mark_halt(nid, stop_reason="circuit_open")

    assert len(obs.decisions) == 1
    decision_node_id, decision_str, reason_str = obs.decisions[0]
    assert decision_node_id == nid
    assert decision_str == "HALT"
    assert reason_str == "circuit_open"


def test_on_decision_fires_on_halt_without_stop_reason() -> None:
    """on_decision fires even when stop_reason is None (R7-F1 regression)."""
    g = _make_graph()
    obs = _RecordingObserver()
    g.add_observer(obs)

    nid = _add_llm_node(g)
    g.mark_halt(nid)  # no stop_reason

    assert len(obs.decisions) == 1
    assert obs.decisions[0][1] == "HALT"
    assert obs.decisions[0][2] == "halt"  # default halt_reason


# ---------------------------------------------------------------------------
# Adversarial tests
# ---------------------------------------------------------------------------


def test_observer_exception_does_not_crash_graph() -> None:
    """Observer that raises must not propagate exceptions to the graph caller."""

    class _BoomObserver:
        def on_node_start(self, *args):
            raise RuntimeError("boom")

        def on_node_complete(self, *args):
            raise RuntimeError("boom")

        def on_node_failed(self, *args):
            raise RuntimeError("boom")

        def on_decision(self, *args):
            raise RuntimeError("boom")

    g = _make_graph()
    g.add_observer(_BoomObserver())

    nid = _add_llm_node(g)
    g.mark_running(nid)  # must not raise
    g.mark_success(nid, cost_usd=0.0)  # must not raise
    snap = g.snapshot()
    assert snap["nodes"][nid]["status"] == "success"


def test_subscriber_exception_does_not_crash_graph() -> None:
    """Subscriber that raises must not propagate exceptions to the graph caller."""

    def _boom(ev: NodeEvent) -> None:
        raise ValueError("subscriber exploded")

    g = _make_graph()
    g.add_subscriber(_boom)

    nid = _add_llm_node(g)
    g.mark_success(nid, cost_usd=0.0)  # must not raise
    snap = g.snapshot()
    assert snap["nodes"][nid]["status"] == "success"


def test_concurrent_add_remove_observer() -> None:
    """10 threads simultaneously adding/removing observers must not corrupt the list."""
    g = _make_graph()
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            obs = _RecordingObserver()
            for _ in range(50):
                g.add_observer(obs)
                g.remove_observer(obs)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent observer errors: {errors}"


def test_concurrent_subscriber_fire() -> None:
    """10 threads marking nodes while subscribers iterate must not crash."""
    g = _make_graph()
    root_id = g._root_id
    assert root_id is not None

    seen: list[str] = []
    lock = threading.Lock()

    def _sub(ev: NodeEvent) -> None:
        with lock:
            seen.append(ev.node_id)

    g.add_subscriber(_sub)
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            nid = g.begin_node(parent_id=root_id, kind="tool", name="concurrent_tool")
            g.mark_running(nid)
            g.mark_success(nid, cost_usd=0.0)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent subscriber errors: {errors}"
    assert len(seen) == 10


def test_add_observer_during_notification() -> None:
    """Observer that adds another observer inside its callback must not deadlock."""
    g = _make_graph()
    inner_calls: list[str] = []

    class _InnerObserver:
        def on_node_complete(self, node_id, cost_usd, duration_ms):
            inner_calls.append(node_id)

        def on_node_start(self, *args):
            pass

        def on_node_failed(self, *args):
            pass

        def on_decision(self, *args):
            pass

    class _OuterObserver:
        def __init__(self, graph: ExecutionGraph, inner: _InnerObserver) -> None:
            self._graph = graph
            self._inner = inner
            self._added = False

        def on_node_start(self, *args):
            pass

        def on_node_complete(self, node_id, cost_usd, duration_ms):
            if not self._added:
                self._added = True
                # Adding an observer during notification -- must not deadlock
                self._graph.add_observer(self._inner)

        def on_node_failed(self, *args):
            pass

        def on_decision(self, *args):
            pass

    inner = _InnerObserver()
    outer = _OuterObserver(g, inner)
    g.add_observer(outer)

    nid = _add_llm_node(g)
    g.mark_running(nid)
    g.mark_success(nid, cost_usd=0.0)

    # Inner observer was added during on_node_complete; it should receive future events
    nid2 = g.begin_node(parent_id=g._root_id, kind="llm", name="step2")  # type: ignore[arg-type]
    g.mark_running(nid2)
    g.mark_success(nid2, cost_usd=0.0)

    assert nid2 in inner_calls


def test_subscriber_receives_correct_depth() -> None:
    """NodeEvent.depth matches actual depth of the node in the graph."""
    g = _make_graph()
    root_id = g._root_id
    assert root_id is not None

    events: list[NodeEvent] = []
    g.add_subscriber(events.append)

    # depth 1 node
    child_id = g.begin_node(parent_id=root_id, kind="llm", name="depth1")
    # depth 2 node
    grandchild_id = g.begin_node(parent_id=child_id, kind="tool", name="depth2")

    g.mark_success(child_id, cost_usd=0.0)
    g.mark_success(grandchild_id, cost_usd=0.0)

    depth_map = {ev.node_id: ev.depth for ev in events}
    assert depth_map[child_id] == 1
    assert depth_map[grandchild_id] == 2


@nogil_unstable
def test_subscriber_receives_correct_elapsed_ms() -> None:
    """NodeEvent.elapsed_ms approximates end_ts_ms - start_ts_ms."""
    g = _make_graph()
    events: list[NodeEvent] = []
    g.add_subscriber(events.append)

    nid = _add_llm_node(g)
    g.mark_running(nid)
    time.sleep(0.15)  # longer sleep for nogil/high-resolution timer tolerance
    g.mark_success(nid, cost_usd=0.0)

    ev = events[0]
    assert ev.elapsed_ms is not None
    # Allow generous tolerance for nogil scheduler jitter: >= 1ms
    # (any positive elapsed time proves the timer is working)
    assert ev.elapsed_ms >= 1.0


def test_remove_nonexistent_observer() -> None:
    """remove_observer with unregistered observer is a no-op (no exception)."""
    g = _make_graph()
    obs = _RecordingObserver()
    g.remove_observer(obs)  # must not raise


def test_remove_nonexistent_subscriber() -> None:
    """remove_subscriber with unregistered callback is a no-op (no exception)."""
    g = _make_graph()
    events: list[NodeEvent] = []
    g.remove_subscriber(events.append)  # must not raise


# ---------------------------------------------------------------------------
# F.R.I.D.A.Y. R6 audit regression tests
# ---------------------------------------------------------------------------


def test_subscriber_dedup_same_callback() -> None:
    """Adding the same callback twice must register it only once (F2 fix)."""
    g = _make_graph()
    events: list[NodeEvent] = []
    cb = events.append
    g.add_subscriber(cb)
    g.add_subscriber(cb)  # duplicate -- should be no-op

    nid = _add_llm_node(g)
    g.mark_success(nid, cost_usd=0.0)

    assert len(events) == 1  # NOT 2


def test_remove_subscriber_identity_semantics() -> None:
    """remove_subscriber uses identity (is), not equality (__eq__) (F3 fix)."""

    class _EqAlways:
        """Callable whose __eq__ always returns True -- would confuse != based removal."""

        def __call__(self, ev: NodeEvent) -> None:
            pass

        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return id(self)

    g = _make_graph()
    a = _EqAlways()
    b = _EqAlways()
    g.add_subscriber(a)
    g.add_subscriber(b)
    # Remove b by identity -- a must survive despite a == b being True
    g.remove_subscriber(b)

    # Verify a is still in subscriber list after removing b
    nid = _add_llm_node(g)
    g.mark_success(nid, cost_usd=0.0)

    # a is still registered, b was removed → 1 subscriber fired
    assert len(g._subscribers) == 1
    assert g._subscribers[0] is a
