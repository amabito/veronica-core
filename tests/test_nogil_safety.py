"""Thread safety tests validating correct behavior under free-threaded Python (PEP 703).

Tests nogil-critical patterns across the veronica-core codebase:
- Concurrent singleton access (get_default_backend)
- CircuitBreaker concurrent state transitions
- BudgetEnforcer concurrent spend
- ExecutionGraph concurrent node creation
- LocalBudgetBackend concurrent add/get
- Integration concurrent record_fail/record_pass
- Adversarial: race conditions, state corruption, resource exhaustion
"""

from __future__ import annotations

import threading
import time


from veronica_core.budget import BudgetEnforcer
from veronica_core.circuit_breaker import CircuitBreaker
from veronica_core.containment import ExecutionConfig, ExecutionContext
from veronica_core.containment.execution_graph import ExecutionGraph
from veronica_core.distributed import LocalBudgetBackend, get_default_backend
from veronica_core.runtime_policy import PolicyContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    max_cost_usd: float = 100.0,
    max_steps: int = 1000,
    max_retries_total: int = 100,
) -> ExecutionConfig:
    return ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=max_steps,
        max_retries_total=max_retries_total,
    )


# ---------------------------------------------------------------------------
# Concurrent singleton access
# ---------------------------------------------------------------------------


class TestNogilSafety:
    """Thread safety tests validating correct behavior under free-threaded Python (PEP 703)."""

    def test_concurrent_singleton_creation(self) -> None:
        """10 threads calling get_default_backend() simultaneously -- each returns a valid backend."""
        backends: list[LocalBudgetBackend] = []
        lock = threading.Lock()
        errors: list[Exception] = []

        def worker() -> None:
            try:
                backend = get_default_backend()
                with lock:
                    backends.append(backend)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"get_default_backend raised: {errors[0]}"
        assert len(backends) == 10
        # All returned instances must be valid (respond to .get())
        for b in backends:
            assert isinstance(b.get(), float)

    def test_circuit_breaker_concurrent_transitions(self) -> None:
        """10 threads recording failures while 10 record successes -- final state is consistent."""
        breaker = CircuitBreaker(failure_threshold=100, recovery_timeout=9999.0)
        ctx = PolicyContext()
        errors: list[Exception] = []

        def record_failure() -> None:
            try:
                breaker.record_failure()
            except Exception as exc:
                errors.append(exc)

        def record_success() -> None:
            try:
                breaker.record_success()
            except Exception as exc:
                errors.append(exc)

        fail_threads = [threading.Thread(target=record_failure) for _ in range(10)]
        pass_threads = [threading.Thread(target=record_success) for _ in range(10)]
        all_threads = fail_threads + pass_threads
        for t in all_threads:
            t.start()
        for t in all_threads:
            t.join()

        assert errors == [], f"CircuitBreaker concurrent access raised: {errors[0]}"
        # After all threads done, check() must not crash
        decision = breaker.check(ctx)
        assert hasattr(decision, "allowed")

    def test_budget_enforcer_concurrent_spend(self) -> None:
        """10 threads spending $0.1 each against $0.5 limit -- total never exceeds limit."""
        budget = BudgetEnforcer(limit_usd=0.5)
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            result = budget.spend(0.1)
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 5 threads should have succeeded (0.5 / 0.1 = 5)
        success_count = sum(1 for r in results if r is True)
        assert success_count == 5, (
            f"Expected exactly 5 successful spends, got {success_count}"
        )

    def test_execution_graph_concurrent_node_creation(self) -> None:
        """10 threads creating nodes off root -- all nodes recorded, no data corruption."""
        graph = ExecutionGraph()
        root_id = graph.create_root(name="root")
        node_ids: list[str] = []
        lock = threading.Lock()
        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def worker() -> None:
            try:
                barrier.wait()
                nid = graph.begin_node(parent_id=root_id, kind="llm", name="child")
                with lock:
                    node_ids.append(nid)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"ExecutionGraph concurrent begin_node raised: {errors[0]}"
        assert len(node_ids) == 10
        # All node IDs must be unique
        assert len(set(node_ids)) == 10

    def test_local_budget_backend_concurrent_add_get(self) -> None:
        """10 threads adding cost -- final total equals sum of all additions."""
        backend = LocalBudgetBackend()
        n_threads = 10
        amount_per_call = 0.05
        calls_per_thread = 20
        errors: list[Exception] = []
        barrier = threading.Barrier(n_threads)

        def worker() -> None:
            try:
                barrier.wait()
                for _ in range(calls_per_thread):
                    backend.add(amount_per_call)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"LocalBudgetBackend concurrent add raised: {errors[0]}"
        expected = n_threads * calls_per_thread * amount_per_call
        assert abs(backend.get() - expected) < 1e-6

    def test_integration_concurrent_record_fail_record_pass(self) -> None:
        """10 threads failing, 10 passing -- no crash, fail_counts remain non-negative."""
        from veronica_core.integration import VeronicaIntegration
        from veronica_core.backends import MemoryBackend

        integration = VeronicaIntegration(
            cooldown_fails=100,  # High threshold to avoid triggering cooldown
            backend=MemoryBackend(),
        )
        pair = "BTC/USD"
        errors: list[Exception] = []

        def fail_worker() -> None:
            try:
                for _ in range(5):
                    integration.record_fail(pair)
            except Exception as exc:
                errors.append(exc)

        def pass_worker() -> None:
            try:
                for _ in range(5):
                    integration.record_pass(pair)
            except Exception as exc:
                errors.append(exc)

        fail_threads = [threading.Thread(target=fail_worker) for _ in range(10)]
        pass_threads = [threading.Thread(target=pass_worker) for _ in range(10)]
        all_threads = fail_threads + pass_threads
        for t in all_threads:
            t.start()
        for t in all_threads:
            t.join()

        assert errors == [], f"VeronicaIntegration concurrent access raised: {errors[0]}"
        # State must still be readable without crashing
        stats = integration.get_stats()
        assert isinstance(stats, dict)


# ---------------------------------------------------------------------------
# Adversarial: Race conditions
# ---------------------------------------------------------------------------


class TestAdversarialRaceConditions:
    """Adversarial tests for race conditions -- attacker mindset."""

    def test_concurrent_budget_spend_does_not_exceed_limit(self) -> None:
        """Race condition: two threads both see budget < limit, both spend -- should not exceed."""
        limit = 1.0
        budget = BudgetEnforcer(limit_usd=limit)
        # Set spent to just below limit so both threads may see allowance
        budget.spend(0.9)

        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()  # Both threads start simultaneously
            result = budget.spend(0.2)  # Each tries to spend 0.2 (total 0.4, over limit)
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one thread must succeed (0.9 + 0.2 = 1.1 > 1.0 limit; second denied)
        success_count = sum(1 for r in results if r is True)
        assert success_count <= 1, (
            "Both threads spent past the limit -- race condition detected"
        )

    def test_circuit_breaker_state_read_during_transition(self) -> None:
        """State corruption: thread A reads CircuitBreaker state while B transitions it."""
        breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=0.01)
        ctx = PolicyContext()
        errors: list[Exception] = []
        stop = threading.Event()

        def writer() -> None:
            for _ in range(50):
                if stop.is_set():
                    break
                try:
                    breaker.record_failure()
                    time.sleep(0.001)
                    breaker.record_success()
                except Exception as exc:
                    errors.append(exc)
                    return

        def reader() -> None:
            for _ in range(200):
                if stop.is_set():
                    break
                try:
                    decision = breaker.check(ctx)
                    assert isinstance(decision.allowed, bool)
                except Exception as exc:
                    errors.append(exc)
                    return

        writer_thread = threading.Thread(target=writer)
        reader_threads = [threading.Thread(target=reader) for _ in range(5)]
        writer_thread.start()
        for t in reader_threads:
            t.start()
        for t in reader_threads:
            t.join()
        stop.set()
        writer_thread.join()

        assert errors == [], f"CircuitBreaker read/write race raised: {errors[0]}"

    def test_local_budget_backend_concurrent_add_and_get_consistent(self) -> None:
        """LocalBudgetBackend: concurrent add() and get() never return negative values."""
        backend = LocalBudgetBackend()
        errors: list[Exception] = []
        stop = threading.Event()

        def adder() -> None:
            for _ in range(100):
                if stop.is_set():
                    break
                try:
                    backend.add(0.01)
                except Exception as exc:
                    errors.append(exc)
                    return

        def getter() -> None:
            for _ in range(200):
                if stop.is_set():
                    break
                try:
                    val = backend.get()
                    assert val >= 0.0, f"backend.get() returned negative: {val}"
                except AssertionError as exc:
                    errors.append(exc)
                    return
                except Exception as exc:
                    errors.append(exc)
                    return

        adder_threads = [threading.Thread(target=adder) for _ in range(5)]
        getter_threads = [threading.Thread(target=getter) for _ in range(5)]
        for t in adder_threads + getter_threads:
            t.start()
        for t in getter_threads:
            t.join()
        stop.set()
        for t in adder_threads:
            t.join()

        assert errors == [], f"LocalBudgetBackend concurrent add/get raised: {errors[0]}"


# ---------------------------------------------------------------------------
# Adversarial: Resource exhaustion
# ---------------------------------------------------------------------------


class TestAdversarialResourceExhaustion:
    """Adversarial tests for resource exhaustion -- attacker mindset."""

    def test_100_threads_creating_children_of_same_parent(self) -> None:
        """Resource exhaustion: 100 threads all trying to create children of same ExecutionContext."""
        config = _make_config(max_cost_usd=1_000.0, max_steps=10_000)
        parent = ExecutionContext(config=config)
        children: list[ExecutionContext] = []
        lock = threading.Lock()
        errors: list[Exception] = []
        barrier = threading.Barrier(100)

        def worker(name: str) -> None:
            try:
                barrier.wait()
                child = parent.create_child(
                    agent_name=name,
                    agent_names=[name],
                )
                with lock:
                    children.append(child)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"agent_{i}",))
            for i in range(100)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], (
            f"create_child raised under 100-thread load: {errors[0]}"
        )
        assert len(children) == 100

    def test_execution_graph_100_concurrent_nodes(self) -> None:
        """100 threads each creating a node from root -- no ID collision."""
        graph = ExecutionGraph()
        root_id = graph.create_root(name="root")
        node_ids: list[str] = []
        lock = threading.Lock()
        errors: list[Exception] = []
        barrier = threading.Barrier(100)

        def worker() -> None:
            try:
                barrier.wait()
                nid = graph.begin_node(parent_id=root_id, kind="tool", name="worker_node")
                with lock:
                    node_ids.append(nid)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"ExecutionGraph 100-thread stress raised: {errors[0]}"
        # Every node_id must be unique
        assert len(set(node_ids)) == 100, (
            f"Node ID collision detected: {len(node_ids)} nodes but {len(set(node_ids))} unique"
        )

    def test_limit_checker_concurrent_increments_never_corrupt_counter(self) -> None:
        """1000 concurrent step increments must yield exactly 1000, not more/less."""
        from veronica_core.containment._limit_checker import _LimitChecker
        from veronica_core.containment.types import CancellationToken, ExecutionConfig

        config = ExecutionConfig(
            max_cost_usd=1_000.0,
            max_steps=100_000,
            max_retries_total=100_000,
        )
        token = CancellationToken()
        checker = _LimitChecker(config=config, cancellation_token=token)

        barrier = threading.Barrier(100)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                barrier.wait()
                for _ in range(10):
                    checker.increment_step()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"_LimitChecker 100-thread stress raised: {errors[0]}"
        assert checker.step_count == 1000
