"""v0.10.4 regression tests — Fix 6-B, Fix 4-G, Fix 3-A.

Test matrix:
  - Fix 6-B: veronica_guard creates a fresh container per call (state isolation)
  - Fix 4-G: AIcontainer.reset() and check() are protected by a threading.Lock
  - Fix 3-A: mark_success() feeds divergence detection (not only mark_running)
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from veronica_core import BudgetEnforcer, RetryContainer, AgentStepGuard
from veronica_core.container import AIcontainer
from veronica_core.containment.execution_graph import ExecutionGraph
from veronica_core.inject import VeronicaHalt, get_active_container, veronica_guard


# ---------------------------------------------------------------------------
# Fix 6-B: per-call container isolation
# ---------------------------------------------------------------------------


class TestVeronicaGuardPerCallContainer:
    """Each invocation of a veronica_guard-wrapped function must start with
    a fresh container so that state (budget, retries, steps) never leaks
    between calls."""

    def test_separate_containers_each_call(self) -> None:
        """Containers retrieved from two separate calls must not be the same object."""
        containers: list = []

        @veronica_guard(max_cost_usd=5.0, max_steps=10, max_retries_total=3)
        def fn() -> None:
            containers.append(get_active_container())

        fn()
        fn()

        assert len(containers) == 2
        assert containers[0] is not containers[1], (
            "Each call must receive its own container — state must not be shared"
        )

    def test_budget_state_not_shared_across_calls(self) -> None:
        """Spending budget inside one call must not affect the next call's budget."""
        spent: list[float] = []

        @veronica_guard(max_cost_usd=1.0, max_steps=10, max_retries_total=3)
        def fn() -> None:
            container = get_active_container()
            assert container is not None
            # Exhaust the budget
            container.budget.spend(0.99)  # type: ignore[union-attr]
            spent.append(container.budget.spent_usd)  # type: ignore[union-attr]

        fn()
        # Second call: budget should start fresh (not near-exhausted)
        fn()

        assert len(spent) == 2
        assert spent[0] == pytest.approx(0.99)
        # Second call starts from 0 and spends 0.99 — not cumulative
        assert spent[1] == pytest.approx(0.99)

    def test_guard_raises_halt_when_denied(self) -> None:
        """VeronicaHalt is raised when the initial check() denies execution.

        max_steps=0 means _current_step (0) >= max_steps (0) immediately.
        """
        @veronica_guard(max_cost_usd=1.0, max_steps=0, max_retries_total=3)
        def fn() -> None:  # pragma: no cover
            pass

        with pytest.raises(VeronicaHalt):
            fn()

    def test_return_decision_on_denial(self) -> None:
        """return_decision=True returns PolicyDecision instead of raising."""
        @veronica_guard(
            max_cost_usd=1.0,
            max_steps=0,
            max_retries_total=3,
            return_decision=True,
        )
        def fn():  # type: ignore[return]
            pass  # pragma: no cover

        result = fn()
        assert hasattr(result, "allowed")
        assert result.allowed is False

    def test_wrapper_has_no_container_attribute(self) -> None:
        """wrapper._container must not exist — per-call design removes it."""
        @veronica_guard(max_cost_usd=1.0, max_steps=10, max_retries_total=3)
        def fn() -> None:
            pass

        assert not hasattr(fn, "_container"), (
            "wrapper._container was removed in Fix 6-B; "
            "tests relying on it must use get_active_container() instead"
        )


# ---------------------------------------------------------------------------
# Fix 4-G: AIcontainer lock
# ---------------------------------------------------------------------------


class TestAIcontainerLock:
    """AIcontainer.reset() and check() must be protected by a threading.Lock
    so that concurrent calls from different threads are race-free."""

    def test_concurrent_calls_succeed_without_deadlock(self) -> None:
        """Concurrent reset() and check() calls must complete without deadlock."""
        container = AIcontainer(budget=BudgetEnforcer(limit_usd=1.0))
        errors: list[Exception] = []

        def mixed_calls() -> None:
            try:
                for _ in range(10):
                    container.check()
                    container.reset()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=mixed_calls) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent calls raised: {errors}"

    def test_concurrent_reset_does_not_raise(self) -> None:
        """Concurrent reset() calls from multiple threads must not raise."""
        container = AIcontainer(
            budget=BudgetEnforcer(limit_usd=10.0),
            retry=RetryContainer(max_retries=5),
            step_guard=AgentStepGuard(max_steps=20),
        )
        errors: list[Exception] = []

        def reset_many() -> None:
            try:
                for _ in range(50):
                    container.reset()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=reset_many) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent reset() raised: {errors}"

    def test_concurrent_check_does_not_raise(self) -> None:
        """Concurrent check() calls from multiple threads must not raise."""
        container = AIcontainer(budget=BudgetEnforcer(limit_usd=100.0))
        errors: list[Exception] = []

        def check_many() -> None:
            try:
                for _ in range(50):
                    container.check()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=check_many) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent check() raised: {errors}"

    def test_reset_rebuilds_pipeline(self) -> None:
        """reset() must still rebuild the pipeline correctly under the lock."""
        container = AIcontainer(budget=BudgetEnforcer(limit_usd=1.0))
        before = container._pipeline
        container.reset()
        after = container._pipeline
        # Pipeline is rebuilt — it may be a new object
        assert after is not None
        assert container.check().allowed


# ---------------------------------------------------------------------------
# Fix 3-A: mark_success divergence detection
# ---------------------------------------------------------------------------


class TestMarkSuccessDivergence:
    """mark_success() must feed divergence detection, not only mark_running().

    The bug: repeated successful tool/llm calls in a loop could avoid
    divergence detection if mark_running was skipped (already-running nodes)
    or if the agent called mark_success without mark_running.
    """

    def test_mark_success_emits_divergence_for_repeated_tool(self) -> None:
        """Repeated mark_success for the same tool signature must trigger
        divergence_suspected after hitting the threshold (3 for tools)."""
        graph = ExecutionGraph(chain_id="test-chain-3a")
        root_id = graph.create_root(name="agent_run")

        divergence_events: list = []
        threshold = 3  # tool threshold per _diverge_thresholds

        for _ in range(threshold + 1):
            node_id = graph.begin_node(
                parent_id=root_id, kind="tool", name="web_search"
            )
            graph.mark_success(node_id, cost_usd=0.0)
            divergence_events.extend(graph.drain_divergence_events())

        assert any(
            e["event_type"] == "divergence_suspected" for e in divergence_events
        ), (
            "mark_success must feed divergence detection; "
            f"no divergence_suspected event found after {threshold + 1} repeats. "
            f"Events seen: {divergence_events}"
        )

    def test_mark_success_divergence_contains_expected_fields(self) -> None:
        """Divergence event from mark_success must have all required fields."""
        graph = ExecutionGraph(chain_id="test-fields")
        root_id = graph.create_root(name="agent_run")

        for _ in range(4):  # tool threshold = 3
            node_id = graph.begin_node(
                parent_id=root_id, kind="tool", name="calc"
            )
            graph.mark_success(node_id, cost_usd=0.001)

        events = graph.drain_divergence_events()
        div = next(
            (e for e in events if e["event_type"] == "divergence_suspected"), None
        )
        assert div is not None
        assert div["severity"] == "warn"
        assert div["signature"] == ["tool", "calc"]
        assert isinstance(div["repeat_count"], int)
        assert div["chain_id"] == "test-fields"

    def test_mark_success_divergence_deduplication(self) -> None:
        """Once emitted for a signature, further mark_success calls for the
        same signature must NOT emit duplicate events."""
        graph = ExecutionGraph(chain_id="test-dedup")
        root_id = graph.create_root(name="agent_run")

        # Trigger divergence
        for _ in range(4):
            nid = graph.begin_node(parent_id=root_id, kind="tool", name="search")
            graph.mark_success(nid, cost_usd=0.0)
        first_batch = graph.drain_divergence_events()

        # Additional calls — must NOT produce more events for same signature
        for _ in range(4):
            nid = graph.begin_node(parent_id=root_id, kind="tool", name="search")
            graph.mark_success(nid, cost_usd=0.0)
        second_batch = graph.drain_divergence_events()

        assert sum(
            1 for e in first_batch if e["event_type"] == "divergence_suspected"
        ) == 1, "Exactly one divergence event expected in first batch"
        assert not any(
            e["event_type"] == "divergence_suspected" for e in second_batch
        ), "No duplicate divergence events expected after deduplication"

    def test_mark_success_does_not_trigger_for_system_nodes(self) -> None:
        """system-kind nodes have threshold=999 and must never trigger divergence
        via mark_success for reasonable loop counts."""
        graph = ExecutionGraph(chain_id="test-system")
        root_id = graph.create_root(name="agent_run")

        for _ in range(10):
            nid = graph.begin_node(
                parent_id=root_id, kind="system", name="checkpoint"
            )
            graph.mark_success(nid, cost_usd=0.0)

        events = graph.drain_divergence_events()
        assert not any(
            e["event_type"] == "divergence_suspected" for e in events
        ), "system nodes must not trigger divergence at reasonable loop counts"
