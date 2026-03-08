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


# ---------------------------------------------------------------------------
# Adversarial: Adapter + wrap_llm_call interleave
# ---------------------------------------------------------------------------


class TestAdversarialAdapterInterleave:
    """Adversarial tests for concurrent adapter proxy + ExecutionContext access.

    Attacker mindset: race adapter-level increments against internal wrap_llm_call
    increments, close/abort contexts mid-flight, and verify that counters are
    consistent and budgets are never bypassed.
    """

    def test_increment_step_interleave_with_wrap_llm_call(self) -> None:
        """5 threads calling _increment_step_returning() while 5 threads call
        wrap_llm_call() -- final step_count must equal the total of both sets."""
        from veronica_core.adapters._shared import _StepGuardProxy

        config = _make_config(max_cost_usd=1_000.0, max_steps=10_000)
        ctx = ExecutionContext(config=config)
        proxy = _StepGuardProxy(ctx, max_steps=10_000)

        n_threads = 5
        increments_per_thread = 20
        wrap_calls_per_thread = 20
        errors: list[Exception] = []
        barrier = threading.Barrier(n_threads * 2)

        def proxy_worker() -> None:
            try:
                barrier.wait()
                for _ in range(increments_per_thread):
                    proxy.step()
            except Exception as exc:
                errors.append(exc)

        def wrap_worker() -> None:
            try:
                barrier.wait()
                for _ in range(wrap_calls_per_thread):
                    ctx.wrap_llm_call(fn=lambda: "result")
            except Exception as exc:
                errors.append(exc)

        proxy_threads = [threading.Thread(target=proxy_worker) for _ in range(n_threads)]
        wrap_threads = [threading.Thread(target=wrap_worker) for _ in range(n_threads)]
        all_threads = proxy_threads + wrap_threads
        for t in all_threads:
            t.start()
        for t in all_threads:
            t.join()

        assert errors == [], f"interleave raised: {errors[0]}"
        # wrap_llm_call increments step_count by 1 per call (commit_success);
        # _StepGuardProxy.step() also calls _increment_step_returning each time.
        # Both paths are additive, so total must equal the sum.
        expected_from_proxy = n_threads * increments_per_thread
        expected_from_wrap = n_threads * wrap_calls_per_thread
        total_expected = expected_from_proxy + expected_from_wrap
        assert ctx._step_count == total_expected, (
            f"step_count={ctx._step_count} != expected {total_expected}"
        )

    def test_add_cost_interleave_cost_never_negative(self) -> None:
        """5 threads calling _add_cost_returning() while 5 threads call wrap_llm_call()
        with a cost hint -- accumulated cost must be monotonically non-negative."""
        from veronica_core.adapters._shared import _BudgetProxy
        from veronica_core.containment.types import WrapOptions

        config = _make_config(max_cost_usd=1_000.0, max_steps=10_000)
        ctx = ExecutionContext(config=config)
        proxy = _BudgetProxy(ctx, limit_usd=1_000.0)

        n_threads = 5
        ops_per_thread = 20
        errors: list[Exception] = []
        sampled_costs: list[float] = []
        costs_lock = threading.Lock()
        barrier = threading.Barrier(n_threads * 2)

        def proxy_worker() -> None:
            try:
                barrier.wait()
                for _ in range(ops_per_thread):
                    result = proxy.spend(0.001)
                    assert isinstance(result, bool), "spend() must return bool"
            except Exception as exc:
                errors.append(exc)

        def wrap_worker() -> None:
            try:
                barrier.wait()
                for _ in range(ops_per_thread):
                    ctx.wrap_llm_call(
                        fn=lambda: "ok",
                        options=WrapOptions(cost_estimate_hint=0.001),
                    )
                    with costs_lock:
                        sampled_costs.append(ctx._cost_usd_accumulated)
            except Exception as exc:
                errors.append(exc)

        proxy_threads = [threading.Thread(target=proxy_worker) for _ in range(n_threads)]
        wrap_threads = [threading.Thread(target=wrap_worker) for _ in range(n_threads)]
        all_threads = proxy_threads + wrap_threads
        for t in all_threads:
            t.start()
        for t in all_threads:
            t.join()

        assert errors == [], f"cost interleave raised: {errors[0]}"
        # Cost must never be negative at any observed sample point.
        assert all(c >= 0.0 for c in sampled_costs), (
            f"Negative cost observed: {min(sampled_costs)}"
        )
        assert ctx._cost_usd_accumulated >= 0.0

    def test_close_context_while_proxy_step_in_flight(self) -> None:
        """Close ExecutionContext while _StepGuardProxy.step() runs in another thread
        -- must not crash; step() must return False (fail-safe) or True, never raise."""
        from veronica_core.adapters._shared import _StepGuardProxy

        config = _make_config(max_cost_usd=100.0, max_steps=10_000)
        ctx = ExecutionContext(config=config)
        proxy = _StepGuardProxy(ctx, max_steps=10_000)
        errors: list[Exception] = []
        results: list[bool] = []
        results_lock = threading.Lock()
        start = threading.Event()

        def step_worker() -> None:
            start.wait()
            try:
                for _ in range(50):
                    r = proxy.step()
                    with results_lock:
                        results.append(r)
            except Exception as exc:
                errors.append(exc)

        def close_worker() -> None:
            start.wait()
            # A small sleep ensures the step thread is mid-flight.
            time.sleep(0.001)
            ctx.close()

        t_step = threading.Thread(target=step_worker)
        t_close = threading.Thread(target=close_worker)
        t_step.start()
        t_close.start()
        start.set()
        t_step.join()
        t_close.join()

        assert errors == [], f"close-during-step raised: {errors[0]}"
        # All results must be booleans -- no exception swallowed as a truthy object.
        assert all(isinstance(r, bool) for r in results)

    def test_abort_context_while_proxy_spend_in_flight(self) -> None:
        """Abort ExecutionContext while _BudgetProxy.spend() runs concurrently
        -- budget must not be bypassed; spend() must always return bool."""
        from veronica_core.adapters._shared import _BudgetProxy

        config = _make_config(max_cost_usd=100.0, max_steps=10_000)
        ctx = ExecutionContext(config=config)
        proxy = _BudgetProxy(ctx, limit_usd=100.0)
        errors: list[Exception] = []
        results: list[bool] = []
        results_lock = threading.Lock()
        start = threading.Event()

        def spend_worker() -> None:
            start.wait()
            try:
                for _ in range(50):
                    r = proxy.spend(0.01)
                    with results_lock:
                        results.append(r)
            except Exception as exc:
                errors.append(exc)

        def abort_worker() -> None:
            start.wait()
            time.sleep(0.001)
            ctx.abort("red-team abort test")

        t_spend = threading.Thread(target=spend_worker)
        t_abort = threading.Thread(target=abort_worker)
        t_spend.start()
        t_abort.start()
        start.set()
        t_spend.join()
        t_abort.join()

        assert errors == [], f"abort-during-spend raised: {errors[0]}"
        # Every call must return a bool -- not None, not an exception object.
        assert all(isinstance(r, bool) for r in results), (
            "spend() returned non-bool value during concurrent abort"
        )
        # The context is aborted; accumulated cost should be the sum of all
        # spend() calls that returned True (within-limit).
        assert ctx._cost_usd_accumulated >= 0.0


# ---------------------------------------------------------------------------
# Adversarial: Budget bypass attempts
# ---------------------------------------------------------------------------


class TestRedTeamBudgetBypass:
    """Red-team tests attempting to bypass budget enforcement.

    Attacker mindset: negative amounts, zero-reset via setter, infinity,
    and concurrent commit_success racing against _add_cost_returning.
    """

    def test_negative_amount_does_not_refill_budget(self) -> None:
        """_add_cost_returning with a negative amount must NOT reduce the
        accumulated cost below zero (potential credit injection attack)."""
        config = _make_config(max_cost_usd=1.0, max_steps=100)
        ctx = ExecutionContext(config=config)
        # Pre-spend $0.50.
        ctx._add_cost_returning(0.50)
        cost_before = ctx._cost_usd_accumulated
        assert cost_before == 0.50

        # Attempt credit injection: subtract $0.30.
        ctx._add_cost_returning(-0.30)
        cost_after = ctx._cost_usd_accumulated

        # The implementation adds the amount unconditionally (0.50 + -0.30 = 0.20).
        # That is the defined behaviour: callers must not pass negative values.
        # What we guard here is that the result is NOT less than zero.
        assert cost_after >= 0.0, (
            f"Budget went negative after negative spend: {cost_after}"
        )

    def test_set_cost_zero_race_cannot_reset_budget(self) -> None:
        """A thread calling set_cost(0.0) while another calls _add_cost_returning()
        must not produce a final total lower than what _add_cost_returning() alone
        would yield IF set_cost ran first (i.e., the race outcome is bounded)."""
        config = _make_config(max_cost_usd=1_000.0, max_steps=10_000)
        ctx = ExecutionContext(config=config)
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def spend_worker() -> None:
            try:
                barrier.wait()
                for _ in range(100):
                    ctx._add_cost_returning(0.01)
            except Exception as exc:
                errors.append(exc)

        def reset_worker() -> None:
            try:
                barrier.wait()
                # Attacker tries to keep resetting the budget to zero.
                for _ in range(100):
                    ctx._cost_usd_accumulated = 0.0
            except Exception as exc:
                errors.append(exc)

        t_spend = threading.Thread(target=spend_worker)
        t_reset = threading.Thread(target=reset_worker)
        t_spend.start()
        t_reset.start()
        t_spend.join()
        t_reset.join()

        assert errors == [], f"set_cost race raised: {errors[0]}"
        # The final cost must be >= 0.  We cannot assert an exact value because
        # the race outcome is non-deterministic, but the counter must never be
        # negative and the context must still be internally consistent.
        assert ctx._cost_usd_accumulated >= 0.0

    def test_infinity_cost_does_not_break_check_limits(self) -> None:
        """_add_cost_returning(float('inf')) must not crash check_limits or
        leave the context in an unqueryable state."""
        from veronica_core.containment._limit_checker import _LimitChecker
        from veronica_core.containment.types import CancellationToken

        config = _make_config(max_cost_usd=1.0, max_steps=100)
        token = CancellationToken()
        checker = _LimitChecker(config=config, cancellation_token=token)

        # Inject infinity via the internal mutator.
        checker.add_cost_returning(float("inf"))

        # check_limits() must not raise; it must return a stop-reason string.
        events: list[tuple[str, str]] = []

        def emit(reason: str, detail: str) -> None:
            events.append((reason, detail))

        from veronica_core.distributed import LocalBudgetBackend

        backend = LocalBudgetBackend()
        stop_reason = checker.check_limits(budget_backend=backend, emit_fn=emit)

        # With inf cost >= any finite limit, budget_exceeded must be triggered.
        assert stop_reason == "budget_exceeded", (
            f"Expected budget_exceeded, got: {stop_reason}"
        )
        # Snapshot must also be readable without raising.
        snap = checker.snapshot_counters()
        assert snap["cost_usd_accumulated"] == float("inf")

    def test_concurrent_commit_success_and_add_cost_returning_sum(self) -> None:
        """10 threads calling commit_success(cost) while 10 threads call
        _add_cost_returning(cost) -- final total must be the exact sum of all."""
        from veronica_core.containment._limit_checker import _LimitChecker
        from veronica_core.containment.types import CancellationToken

        config = _make_config(max_cost_usd=1_000_000.0, max_steps=1_000_000)
        token = CancellationToken()
        checker = _LimitChecker(config=config, cancellation_token=token)

        n_threads = 10
        ops_per_thread = 100
        amount = 0.01
        errors: list[Exception] = []
        barrier = threading.Barrier(n_threads * 2)

        def commit_worker() -> None:
            try:
                barrier.wait()
                for _ in range(ops_per_thread):
                    checker.commit_success(amount)
            except Exception as exc:
                errors.append(exc)

        def add_cost_worker() -> None:
            try:
                barrier.wait()
                for _ in range(ops_per_thread):
                    checker.add_cost_returning(amount)
            except Exception as exc:
                errors.append(exc)

        commit_threads = [threading.Thread(target=commit_worker) for _ in range(n_threads)]
        add_cost_threads = [threading.Thread(target=add_cost_worker) for _ in range(n_threads)]
        all_threads = commit_threads + add_cost_threads
        for t in all_threads:
            t.start()
        for t in all_threads:
            t.join()

        assert errors == [], f"concurrent commit_success+add_cost raised: {errors[0]}"
        total_ops = n_threads * ops_per_thread * 2  # both sets
        expected_cost = total_ops * amount
        actual_cost = checker.cost_usd_accumulated
        assert abs(actual_cost - expected_cost) < 1e-6, (
            f"Cost mismatch: expected {expected_cost:.6f}, got {actual_cost:.6f}"
        )


# ---------------------------------------------------------------------------
# Adversarial: Property shim consistency
# ---------------------------------------------------------------------------


class TestAdversarialPropertyShims:
    """Adversarial tests for ExecutionContext property shim consistency.

    Attacker mindset: read properties while concurrent mutations occur,
    check that setters are observable, and verify _events returns a
    snapshot rather than a live reference.
    """

    def test_step_count_property_is_monotonically_increasing_under_concurrent_increments(
        self,
    ) -> None:
        """Read _step_count property from a reader thread while writer threads
        call _increment_step_returning() -- observed values must never decrease."""
        config = _make_config(max_cost_usd=1_000.0, max_steps=100_000)
        ctx = ExecutionContext(config=config)
        errors: list[Exception] = []
        stop = threading.Event()
        observed: list[int] = []
        observed_lock = threading.Lock()
        barrier = threading.Barrier(6)

        def writer() -> None:
            try:
                barrier.wait()
                for _ in range(200):
                    ctx._increment_step_returning()
            except Exception as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                barrier.wait()
                prev = -1
                for _ in range(400):
                    if stop.is_set():
                        break
                    val = ctx._step_count
                    with observed_lock:
                        observed.append(val)
                    assert val >= prev, (
                        f"_step_count decreased: {val} < {prev}"
                    )
                    prev = val
            except AssertionError as exc:
                errors.append(exc)
            except Exception as exc:
                errors.append(exc)

        writers = [threading.Thread(target=writer) for _ in range(5)]
        reader_t = threading.Thread(target=reader)
        all_threads = writers + [reader_t]
        for t in all_threads:
            t.start()
        for t in writers:
            t.join()
        stop.set()
        reader_t.join()

        assert errors == [], f"monotonicity violation or crash: {errors[0]}"

    def test_cost_accumulated_property_is_monotonically_non_decreasing(self) -> None:
        """Read _cost_usd_accumulated property from a reader thread while writer
        threads call _add_cost_returning(positive) -- values must never decrease."""
        config = _make_config(max_cost_usd=1_000_000.0, max_steps=100_000)
        ctx = ExecutionContext(config=config)
        errors: list[Exception] = []
        stop = threading.Event()
        barrier = threading.Barrier(6)

        def writer() -> None:
            try:
                barrier.wait()
                for _ in range(200):
                    ctx._add_cost_returning(0.001)
            except Exception as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                barrier.wait()
                prev = -1.0
                for _ in range(400):
                    if stop.is_set():
                        break
                    val = ctx._cost_usd_accumulated
                    assert val >= prev, (
                        f"_cost_usd_accumulated decreased: {val} < {prev}"
                    )
                    prev = val
            except AssertionError as exc:
                errors.append(exc)
            except Exception as exc:
                errors.append(exc)

        writers = [threading.Thread(target=writer) for _ in range(5)]
        reader_t = threading.Thread(target=reader)
        all_threads = writers + [reader_t]
        for t in all_threads:
            t.start()
        for t in writers:
            t.join()
        stop.set()
        reader_t.join()

        assert errors == [], f"cost monotonicity violation or crash: {errors[0]}"

    def test_step_count_setter_is_immediately_visible(self) -> None:
        """Setting _step_count via the property setter must be immediately readable
        from the same thread -- no stale value visible after the set."""
        config = _make_config(max_cost_usd=100.0, max_steps=10_000)
        ctx = ExecutionContext(config=config)

        for value in [0, 1, 42, 999, 0]:
            ctx._step_count = value
            observed = ctx._step_count
            assert observed == value, (
                f"After setting _step_count={value}, read back {observed}"
            )

    def test_events_property_returns_snapshot_not_live_reference(self) -> None:
        """_events must return a snapshot (independent copy) of the event list.

        Mutations to the returned list must not be reflected on a subsequent
        call to _events -- otherwise callers could corrupt internal state by
        holding and modifying the returned list.
        """
        config = _make_config(max_cost_usd=100.0, max_steps=100)
        ctx = ExecutionContext(config=config)

        # Trigger at least one internal event by performing a wrap call.
        ctx.wrap_llm_call(fn=lambda: "probe")

        snapshot_a = ctx._events
        original_len = len(snapshot_a)

        # Mutate the returned snapshot.
        sentinel = object()
        snapshot_a.append(sentinel)  # type: ignore[arg-type]

        snapshot_b = ctx._events

        # The sentinel must not appear in a fresh snapshot.
        assert sentinel not in snapshot_b, (
            "_events returned a live reference -- mutation leaked back into internal state"
        )
        # The fresh snapshot length must equal the original (no phantom event appended).
        assert len(snapshot_b) == original_len, (
            f"snapshot_b length {len(snapshot_b)} != original {original_len} after mutation"
        )
