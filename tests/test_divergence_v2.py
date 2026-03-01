"""Tests for v0.11 time-based divergence heuristics: cost-rate and token-velocity."""

from __future__ import annotations

import time


from veronica_core.containment.execution_graph import ExecutionGraph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_graph(**kwargs) -> ExecutionGraph:
    """Create a graph with a single root node already created."""
    g = ExecutionGraph(**kwargs)
    root_id = g.create_root(name="agent_run")
    return g, root_id


def _add_success_node(
    graph: ExecutionGraph,
    parent_id: str,
    kind: str = "llm",
    name: str = "step",
    cost_usd: float = 0.0,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> str:
    node_id = graph.begin_node(parent_id=parent_id, kind=kind, name=name)
    graph.mark_running(node_id)
    # Drain any signature-based divergence events to keep pending list clean.
    graph.drain_divergence_events()
    graph.mark_success(node_id, cost_usd=cost_usd, tokens_in=tokens_in, tokens_out=tokens_out)
    return node_id


# ---------------------------------------------------------------------------
# Cost-rate tests
# ---------------------------------------------------------------------------


def test_cost_rate_exceeded_fires() -> None:
    """COST_RATE_EXCEEDED is emitted when the rate exceeds the threshold."""
    # Very low threshold ensures any nonzero cost/time triggers the event.
    graph, root_id = _make_graph(cost_rate_threshold_usd_per_sec=0.001)

    # Inject a tiny delay so elapsed_sec > 0, then record cost.
    time.sleep(0.01)
    _add_success_node(graph, root_id, cost_usd=1.0)  # 1 USD / ~0.01s >> 0.001 threshold

    events = graph.drain_divergence_events()
    types = [e["event_type"] for e in events]
    assert "COST_RATE_EXCEEDED" in types, f"Expected COST_RATE_EXCEEDED in {types}"


def test_cost_rate_not_fired_below_threshold() -> None:
    """No event when rate stays below the threshold."""
    # Very high threshold so a small cost never triggers.
    graph, root_id = _make_graph(cost_rate_threshold_usd_per_sec=1_000_000.0)

    time.sleep(0.01)
    _add_success_node(graph, root_id, cost_usd=0.01)

    events = graph.drain_divergence_events()
    types = [e["event_type"] for e in events]
    assert "COST_RATE_EXCEEDED" not in types, f"Unexpected COST_RATE_EXCEEDED in {types}"


def test_cost_rate_deduped() -> None:
    """COST_RATE_EXCEEDED fires at most once regardless of how many calls exceed the rate."""
    graph, root_id = _make_graph(cost_rate_threshold_usd_per_sec=0.001)

    time.sleep(0.01)
    _add_success_node(graph, root_id, cost_usd=1.0)
    events1 = graph.drain_divergence_events()
    first_count = sum(1 for e in events1 if e["event_type"] == "COST_RATE_EXCEEDED")

    # Second call also above threshold — should NOT emit again.
    _add_success_node(graph, root_id, cost_usd=1.0)
    events2 = graph.drain_divergence_events()
    second_count = sum(1 for e in events2 if e["event_type"] == "COST_RATE_EXCEEDED")

    assert first_count == 1, f"Expected exactly 1 event on first call, got {first_count}"
    assert second_count == 0, f"Expected 0 events on second call, got {second_count}"


# ---------------------------------------------------------------------------
# Token-velocity tests
# ---------------------------------------------------------------------------


def test_token_velocity_exceeded_fires() -> None:
    """TOKEN_VELOCITY_EXCEEDED is emitted when tokens/sec exceeds the threshold."""
    graph, root_id = _make_graph(token_velocity_threshold=0.1)

    time.sleep(0.01)
    _add_success_node(graph, root_id, tokens_out=1000)  # 1000 tok / ~0.01s >> 0.1 threshold

    events = graph.drain_divergence_events()
    types = [e["event_type"] for e in events]
    assert "TOKEN_VELOCITY_EXCEEDED" in types, f"Expected TOKEN_VELOCITY_EXCEEDED in {types}"


def test_token_velocity_not_fired_below_threshold() -> None:
    """No event when token velocity stays below the threshold."""
    graph, root_id = _make_graph(token_velocity_threshold=1_000_000.0)

    time.sleep(0.01)
    _add_success_node(graph, root_id, tokens_out=5)

    events = graph.drain_divergence_events()
    types = [e["event_type"] for e in events]
    assert "TOKEN_VELOCITY_EXCEEDED" not in types, f"Unexpected TOKEN_VELOCITY_EXCEEDED in {types}"


def test_token_velocity_deduped() -> None:
    """TOKEN_VELOCITY_EXCEEDED fires at most once per chain."""
    graph, root_id = _make_graph(token_velocity_threshold=0.1)

    time.sleep(0.01)
    _add_success_node(graph, root_id, tokens_out=1000)
    events1 = graph.drain_divergence_events()
    first_count = sum(1 for e in events1 if e["event_type"] == "TOKEN_VELOCITY_EXCEEDED")

    _add_success_node(graph, root_id, tokens_out=1000)
    events2 = graph.drain_divergence_events()
    second_count = sum(1 for e in events2 if e["event_type"] == "TOKEN_VELOCITY_EXCEEDED")

    assert first_count == 1, f"Expected exactly 1 event on first call, got {first_count}"
    assert second_count == 0, f"Expected 0 events on second call, got {second_count}"


# ---------------------------------------------------------------------------
# Combined / interaction tests
# ---------------------------------------------------------------------------


def test_cost_rate_and_token_velocity_independent() -> None:
    """Both events are emitted independently when both thresholds are exceeded."""
    graph, root_id = _make_graph(
        cost_rate_threshold_usd_per_sec=0.001,
        token_velocity_threshold=0.1,
    )

    time.sleep(0.01)
    _add_success_node(graph, root_id, cost_usd=1.0, tokens_out=1000)

    events = graph.drain_divergence_events()
    types = [e["event_type"] for e in events]
    assert "COST_RATE_EXCEEDED" in types, f"Expected COST_RATE_EXCEEDED in {types}"
    assert "TOKEN_VELOCITY_EXCEEDED" in types, f"Expected TOKEN_VELOCITY_EXCEEDED in {types}"


def test_drain_clears_both_events() -> None:
    """drain_divergence_events() empties the pending list after collecting both events."""
    graph, root_id = _make_graph(
        cost_rate_threshold_usd_per_sec=0.001,
        token_velocity_threshold=0.1,
    )

    time.sleep(0.01)
    _add_success_node(graph, root_id, cost_usd=1.0, tokens_out=1000)

    first_drain = graph.drain_divergence_events()
    assert len(first_drain) >= 2, f"Expected at least 2 events, got {len(first_drain)}"

    second_drain = graph.drain_divergence_events()
    assert second_drain == [], f"Expected empty list after drain, got {second_drain}"


def test_snapshot_includes_total_tokens_out() -> None:
    """snapshot() aggregates include total_tokens_out with the accumulated count."""
    graph, root_id = _make_graph()

    _add_success_node(graph, root_id, tokens_out=100)
    _add_success_node(graph, root_id, tokens_out=250)

    snap = graph.snapshot()
    total = snap["aggregates"]["total_tokens_out"]
    assert total == 350, f"Expected total_tokens_out=350, got {total}"
