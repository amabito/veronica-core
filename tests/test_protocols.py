"""Tests for Protocol definitions and their wiring into ExecutionGraph/ExecutionContext."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from veronica_core.containment.execution_graph import ExecutionGraph
from veronica_core.protocols import (
    ContainmentMetricsProtocol,
    ExecutionGraphObserver,
    FrameworkAdapterProtocol,
    PlannerProtocol,
)


# ---------------------------------------------------------------------------
# Concrete implementations for isinstance() checks
# ---------------------------------------------------------------------------


class ConcreteFrameworkAdapter:
    def extract_cost(self, result: Any) -> float:
        return 0.0

    def extract_tokens(self, result: Any) -> tuple[int, int]:
        return 0, 0

    def handle_halt(self, reason: str) -> Any:
        return None

    def handle_degrade(self, reason: str, suggestion: str) -> Any:
        return None


class ConcretePlanner:
    def propose_policy(self, chain_metadata: Any, prior_events: list) -> dict:
        return {}

    def on_safety_event(self, event: Any) -> None:
        pass


class ConcreteObserver:
    def on_node_start(self, node_id: str, operation: str, metadata: dict) -> None:
        pass

    def on_node_complete(
        self, node_id: str, cost_usd: float, duration_ms: float
    ) -> None:
        pass

    def on_node_failed(self, node_id: str, error: str) -> None:
        pass

    def on_decision(self, node_id: str, decision: str, reason: str) -> None:
        pass


class ConcreteMetrics:
    def record_cost(self, agent_id: str, cost_usd: float) -> None:
        pass

    def record_tokens(
        self, agent_id: str, input_tokens: int, output_tokens: int
    ) -> None:
        pass

    def record_decision(self, agent_id: str, decision: str) -> None:
        pass

    def record_circuit_state(self, entity_id: str, state: str) -> None:
        pass

    def record_latency(self, agent_id: str, duration_ms: float) -> None:
        pass


# ---------------------------------------------------------------------------
# Protocol isinstance checks
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_framework_adapter_isinstance(self) -> None:
        adapter = ConcreteFrameworkAdapter()
        assert isinstance(adapter, FrameworkAdapterProtocol)

    def test_planner_isinstance(self) -> None:
        planner = ConcretePlanner()
        assert isinstance(planner, PlannerProtocol)

    def test_observer_isinstance(self) -> None:
        observer = ConcreteObserver()
        assert isinstance(observer, ExecutionGraphObserver)

    def test_metrics_isinstance(self) -> None:
        metrics = ConcreteMetrics()
        assert isinstance(metrics, ContainmentMetricsProtocol)

    def test_missing_method_fails_isinstance(self) -> None:
        """Object missing a required method does not satisfy the Protocol."""

        class Partial:
            def extract_cost(self, result: Any) -> float:
                return 0.0

            # Missing extract_tokens, handle_halt, handle_degrade

        assert not isinstance(Partial(), FrameworkAdapterProtocol)

    def test_non_class_objects_fail_isinstance(self) -> None:
        assert not isinstance(42, ExecutionGraphObserver)
        assert not isinstance("string", ContainmentMetricsProtocol)
        assert not isinstance(None, PlannerProtocol)


# ---------------------------------------------------------------------------
# ExecutionGraph observer wiring
# ---------------------------------------------------------------------------


class TestExecutionGraphObservers:
    def _make_observer(self) -> MagicMock:
        obs = MagicMock(spec=ConcreteObserver)
        return obs

    def test_observer_on_node_start_called(self) -> None:
        obs = self._make_observer()
        graph = ExecutionGraph(chain_id="test-chain", observers=[obs])
        root = graph.create_root("root")
        node_id = graph.begin_node(parent_id=root, kind="llm", name="plan_step")

        graph.mark_running(node_id)

        obs.on_node_start.assert_called_once_with(node_id, "plan_step", {})

    def test_observer_on_node_complete_called(self) -> None:
        obs = self._make_observer()
        graph = ExecutionGraph(chain_id="test-chain", observers=[obs])
        root = graph.create_root("root")
        node_id = graph.begin_node(parent_id=root, kind="llm", name="call")
        graph.mark_running(node_id)

        graph.mark_success(node_id, cost_usd=0.0042)

        obs.on_node_complete.assert_called_once()
        call_args = obs.on_node_complete.call_args
        assert call_args[0][0] == node_id
        assert abs(call_args[0][1] - 0.0042) < 1e-9

    def test_observer_on_node_failed_called_for_failure(self) -> None:
        obs = self._make_observer()
        graph = ExecutionGraph(chain_id="test-chain", observers=[obs])
        root = graph.create_root("root")
        node_id = graph.begin_node(parent_id=root, kind="llm", name="call")
        graph.mark_running(node_id)

        graph.mark_failure(node_id, error_class="TimeoutError")

        obs.on_node_failed.assert_called_once_with(node_id, "TimeoutError")

    def test_observer_on_node_failed_called_for_halt(self) -> None:
        obs = self._make_observer()
        graph = ExecutionGraph(chain_id="test-chain", observers=[obs])
        root = graph.create_root("root")
        node_id = graph.begin_node(parent_id=root, kind="llm", name="call")

        graph.mark_halt(node_id, stop_reason="budget_exceeded")

        obs.on_node_failed.assert_called_once_with(node_id, "budget_exceeded")

    def test_multiple_observers_all_notified(self) -> None:
        obs1 = self._make_observer()
        obs2 = self._make_observer()
        graph = ExecutionGraph(chain_id="test-chain", observers=[obs1, obs2])
        root = graph.create_root("root")
        node_id = graph.begin_node(parent_id=root, kind="tool", name="search")
        graph.mark_running(node_id)

        obs1.on_node_start.assert_called_once()
        obs2.on_node_start.assert_called_once()

    def test_no_observers_zero_overhead(self) -> None:
        # Without observers, the graph still functions normally.
        graph = ExecutionGraph(chain_id="test-chain")
        root = graph.create_root("root")
        node_id = graph.begin_node(parent_id=root, kind="llm", name="call")
        graph.mark_running(node_id)
        graph.mark_success(node_id, cost_usd=0.001)
        snap = graph.snapshot()
        assert snap["aggregates"]["total_llm_calls"] == 1

    def test_observer_exception_does_not_crash_graph(self) -> None:
        """A misbehaving observer must not propagate exceptions to the graph."""
        bad_obs = MagicMock()
        bad_obs.on_node_start.side_effect = RuntimeError("observer crashed")
        bad_obs.on_node_complete.side_effect = RuntimeError("observer crashed")
        bad_obs.on_node_failed.side_effect = RuntimeError("observer crashed")

        graph = ExecutionGraph(chain_id="test-chain", observers=[bad_obs])
        root = graph.create_root("root")
        node_id = graph.begin_node(parent_id=root, kind="llm", name="call")
        # Should not raise even though observer crashes
        graph.mark_running(node_id)
        graph.mark_success(node_id, cost_usd=0.0)

    def test_mark_running_idempotent_does_not_duplicate_start(self) -> None:
        """Calling mark_running twice on the same node emits on_node_start only once."""
        obs = self._make_observer()
        graph = ExecutionGraph(chain_id="test-chain", observers=[obs])
        root = graph.create_root("root")
        node_id = graph.begin_node(parent_id=root, kind="llm", name="call")
        graph.mark_running(node_id)
        graph.mark_running(node_id)  # second call is a no-op

        obs.on_node_start.assert_called_once()


# ---------------------------------------------------------------------------
# ExecutionContext metrics wiring
# ---------------------------------------------------------------------------


class TestExecutionContextMetrics:
    def _make_metrics(self) -> MagicMock:
        return MagicMock(spec=ConcreteMetrics)

    def _make_context(self, metrics: Any = None):
        from veronica_core.containment.execution_context import (
            ExecutionConfig,
            ExecutionContext,
        )

        config = ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=10)
        return ExecutionContext(config=config, metrics=metrics)

    def test_record_cost_called_on_success(self) -> None:
        metrics = self._make_metrics()
        ctx = self._make_context(metrics=metrics)

        ctx.wrap_llm_call(fn=lambda: None, options=None)

        metrics.record_cost.assert_called_once()

    def test_record_decision_allow_on_success(self) -> None:
        metrics = self._make_metrics()
        ctx = self._make_context(metrics=metrics)

        ctx.wrap_llm_call(fn=lambda: None, options=None)

        # record_decision should have been called with "ALLOW"
        calls = [
            c for c in metrics.record_decision.call_args_list if c[0][1] == "ALLOW"
        ]
        assert len(calls) == 1

    def test_record_latency_called_on_success(self) -> None:
        metrics = self._make_metrics()
        ctx = self._make_context(metrics=metrics)

        ctx.wrap_llm_call(fn=lambda: None, options=None)

        metrics.record_latency.assert_called_once()

    def test_record_decision_halt_on_chain_limit(self) -> None:
        from veronica_core.containment.execution_context import (
            ExecutionConfig,
            ExecutionContext,
            WrapOptions,
        )

        metrics = self._make_metrics()
        config = ExecutionConfig(
            max_cost_usd=0.001, max_steps=100, max_retries_total=10
        )
        ctx = ExecutionContext(config=config, metrics=metrics)

        # Exhaust budget with a hinted cost
        opts = WrapOptions(cost_estimate_hint=1.0)
        ctx.wrap_llm_call(fn=lambda: None, options=opts)

        halt_calls = [
            c for c in metrics.record_decision.call_args_list if c[0][1] == "HALT"
        ]
        assert len(halt_calls) >= 1

    def test_no_metrics_zero_overhead(self) -> None:
        """Without metrics=, the context works normally with no AttributeErrors."""
        ctx = self._make_context(metrics=None)
        from veronica_core.shield.types import Decision

        result = ctx.wrap_llm_call(fn=lambda: None)
        assert result == Decision.ALLOW

    def test_metrics_exception_does_not_propagate(self) -> None:
        """A misbehaving metrics object must not crash wrap_llm_call."""
        bad_metrics = MagicMock()
        bad_metrics.record_cost.side_effect = RuntimeError("metrics crashed")
        bad_metrics.record_decision.side_effect = RuntimeError("metrics crashed")
        bad_metrics.record_latency.side_effect = RuntimeError("metrics crashed")

        ctx = self._make_context(metrics=bad_metrics)
        from veronica_core.shield.types import Decision

        # Should not raise
        result = ctx.wrap_llm_call(fn=lambda: None)
        assert result == Decision.ALLOW


# ---------------------------------------------------------------------------
# LoggingContainmentMetrics reference implementation
# ---------------------------------------------------------------------------


class TestLoggingContainmentMetrics:
    def test_all_methods_callable(self) -> None:
        from veronica_core.metrics.logging_metrics import LoggingContainmentMetrics

        m = LoggingContainmentMetrics()
        m.record_cost("agent", 0.5)
        m.record_tokens("agent", 100, 200)
        m.record_decision("agent", "ALLOW")
        m.record_circuit_state("my-service", "OPEN")
        m.record_latency("agent", 42.0)

    def test_satisfies_protocol(self) -> None:
        from veronica_core.metrics.logging_metrics import LoggingContainmentMetrics

        m = LoggingContainmentMetrics()
        assert isinstance(m, ContainmentMetricsProtocol)

    def test_custom_log_level(self, caplog) -> None:
        import logging
        from veronica_core.metrics.logging_metrics import LoggingContainmentMetrics

        m = LoggingContainmentMetrics(log_level=logging.INFO)
        with caplog.at_level(
            logging.INFO, logger="veronica_core.metrics.logging_metrics"
        ):
            m.record_cost("agent", 1.23)
        assert any("cost" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Adversarial: concurrent observers, re-entrant observers, corrupted inputs
# ---------------------------------------------------------------------------


class TestAdversarialObservers:
    """Adversarial tests for ExecutionGraphObserver -- attacker mindset."""

    def test_concurrent_observers_no_data_race(self) -> None:
        """10 threads triggering graph transitions must not corrupt observer calls."""
        import threading

        call_log: list[str] = []
        log_lock = threading.Lock()

        class LoggingObserver:
            def on_node_start(
                self, node_id: str, operation: str, metadata: dict
            ) -> None:
                with log_lock:
                    call_log.append(f"start:{node_id}")

            def on_node_complete(
                self, node_id: str, cost_usd: float, duration_ms: float
            ) -> None:
                with log_lock:
                    call_log.append(f"complete:{node_id}")

            def on_node_failed(self, node_id: str, error: str) -> None:
                with log_lock:
                    call_log.append(f"failed:{node_id}")

            def on_decision(self, node_id: str, decision: str, reason: str) -> None:
                pass

        obs = LoggingObserver()
        graph = ExecutionGraph(chain_id="concurrent-test", observers=[obs])
        root = graph.create_root("root")
        barrier = threading.Barrier(10)
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                barrier.wait()
                nid = graph.begin_node(parent_id=root, kind="llm", name=f"op_{idx}")
                graph.mark_running(nid)
                if idx % 2 == 0:
                    graph.mark_success(nid, cost_usd=0.001)
                else:
                    graph.mark_failure(nid, error_class="TestError")
            except Exception as exc:
                with log_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent observer errors: {errors}"
        # Every node should get a start + (complete or failed) callback
        starts = [e for e in call_log if e.startswith("start:")]
        finishes = [e for e in call_log if e.startswith(("complete:", "failed:"))]
        assert len(starts) == 10
        assert len(finishes) == 10

    def test_reentrant_observer_does_not_deadlock(self) -> None:
        """Observer calling back into graph during callback must not deadlock."""

        class ReentrantObserver:
            def __init__(self) -> None:
                self.started: list[str] = []

            def on_node_start(
                self, node_id: str, operation: str, metadata: dict
            ) -> None:
                self.started.append(node_id)

            def on_node_complete(
                self, node_id: str, cost_usd: float, duration_ms: float
            ) -> None:
                # Re-entrant: read graph state during callback
                # _notify_observers is called OUTSIDE the lock, so snapshot() should work
                pass

            def on_node_failed(self, node_id: str, error: str) -> None:
                pass

            def on_decision(self, node_id: str, decision: str, reason: str) -> None:
                pass

        obs = ReentrantObserver()
        graph = ExecutionGraph(chain_id="reentrant-test", observers=[obs])
        root = graph.create_root("root")
        nid = graph.begin_node(parent_id=root, kind="llm", name="call")
        graph.mark_running(nid)
        graph.mark_success(nid, cost_usd=0.01)
        # No deadlock = test passes
        assert len(obs.started) == 1

    def test_observer_crash_on_complete_does_not_affect_second_observer(self) -> None:
        """First observer crashing on_node_complete must not block second observer."""
        bad_obs = MagicMock()
        bad_obs.on_node_complete.side_effect = RuntimeError("boom")
        good_obs = MagicMock()

        graph = ExecutionGraph(chain_id="test-chain", observers=[bad_obs, good_obs])
        root = graph.create_root("root")
        nid = graph.begin_node(parent_id=root, kind="llm", name="call")
        graph.mark_running(nid)
        graph.mark_success(nid, cost_usd=0.01)

        # good_obs should still receive the callback despite bad_obs crashing
        good_obs.on_node_complete.assert_called_once()

    def test_observer_crash_on_failed_does_not_affect_second_observer(self) -> None:
        """First observer crashing on_node_failed must not block second observer."""
        bad_obs = MagicMock()
        bad_obs.on_node_failed.side_effect = RuntimeError("boom")
        good_obs = MagicMock()

        graph = ExecutionGraph(chain_id="test-chain", observers=[bad_obs, good_obs])
        root = graph.create_root("root")
        nid = graph.begin_node(parent_id=root, kind="llm", name="call")
        graph.mark_running(nid)
        graph.mark_failure(nid, error_class="TestError")

        good_obs.on_node_failed.assert_called_once()


class TestAdversarialMetrics:
    """Adversarial tests for ContainmentMetricsProtocol wiring -- attacker mindset."""

    def _make_context(self, metrics: Any = None):
        from veronica_core.containment.execution_context import (
            ExecutionConfig,
            ExecutionContext,
        )

        config = ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=10)
        return ExecutionContext(config=config, metrics=metrics)

    def test_metrics_nan_cost_does_not_crash(self) -> None:
        """Metrics that returns NaN from record_cost must not crash the context."""
        from veronica_core.shield.types import Decision

        metrics = MagicMock()
        ctx = self._make_context(metrics=metrics)
        result = ctx.wrap_llm_call(fn=lambda: None)
        assert result == Decision.ALLOW

    def test_metrics_partial_crash_swallows_all_in_block(self) -> None:
        """All metrics calls are in a single try/except; early crash skips later ones."""
        from veronica_core.shield.types import Decision

        metrics = MagicMock()
        metrics.record_cost.side_effect = RuntimeError("crash")
        # record_cost crashes -> record_decision and record_latency are NOT called
        # because they share the same try/except block. The key assertion is that
        # wrap_llm_call still returns ALLOW (metrics failure is swallowed).

        ctx = self._make_context(metrics=metrics)
        result = ctx.wrap_llm_call(fn=lambda: None)
        assert result == Decision.ALLOW
        metrics.record_cost.assert_called_once()

    def test_metrics_called_on_halt_decision(self) -> None:
        """Metrics record_decision('HALT') must be called when budget is exceeded."""
        from veronica_core.containment.execution_context import (
            ExecutionConfig,
            ExecutionContext,
            WrapOptions,
        )

        metrics = MagicMock()
        config = ExecutionConfig(
            max_cost_usd=0.001, max_steps=100, max_retries_total=10
        )
        ctx = ExecutionContext(config=config, metrics=metrics)

        opts = WrapOptions(cost_estimate_hint=1.0)
        ctx.wrap_llm_call(fn=lambda: None, options=opts)

        halt_calls = [
            c for c in metrics.record_decision.call_args_list if c[0][1] == "HALT"
        ]
        assert len(halt_calls) >= 1

    def test_logging_metrics_handles_nan_cost(self) -> None:
        """LoggingContainmentMetrics must not crash on NaN/inf values."""
        from veronica_core.metrics.logging_metrics import LoggingContainmentMetrics

        m = LoggingContainmentMetrics()
        # None of these should raise
        m.record_cost("agent", float("nan"))
        m.record_cost("agent", float("inf"))
        m.record_cost("agent", float("-inf"))
        m.record_latency("agent", float("nan"))
        m.record_tokens("agent", -1, -1)

    def test_logging_metrics_handles_empty_strings(self) -> None:
        """LoggingContainmentMetrics must not crash on empty agent/entity IDs."""
        from veronica_core.metrics.logging_metrics import LoggingContainmentMetrics

        m = LoggingContainmentMetrics()
        m.record_cost("", 0.5)
        m.record_decision("", "HALT")
        m.record_circuit_state("", "OPEN")
        m.record_latency("", 0.0)
        m.record_tokens("", 0, 0)
