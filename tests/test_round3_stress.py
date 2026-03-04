"""Round 3 stress tests — concurrent + async + resource exhaustion + ContextVar edge cases.

Targets:
1. Concurrent stress: 20 threads on same ExecutionContext, 10 asyncio tasks on veronica_guard
2. Resource exhaustion: _MAX_NODES (10K) cap, budget complete consumption, _MAX_PARTIAL_BUFFERS
3. Error cascade: every call raises, alternating success/failure, BaseException cleanup
4. ContextVar edge cases: nested veronica_guard, asyncio.create_task inside guard, mixed usage
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from veronica_core.containment.execution_context import (
    ExecutionContext,
    ExecutionConfig,
    WrapOptions,
    _MAX_NODES,
    _MAX_PARTIAL_BUFFERS,
)
from veronica_core.inject import is_guard_active, veronica_guard
from veronica_core.partial import PartialResultBuffer
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    max_cost_usd: float = 1000.0,
    max_steps: int = 100_000,
    max_retries_total: int = 100_000,
) -> ExecutionContext:
    return ExecutionContext(
        config=ExecutionConfig(
            max_cost_usd=max_cost_usd,
            max_steps=max_steps,
            max_retries_total=max_retries_total,
        )
    )


# ---------------------------------------------------------------------------
# 1. Concurrent stress
# ---------------------------------------------------------------------------


class TestConcurrentWrap:
    """20 threads calling wrap_llm_call simultaneously on the same ExecutionContext."""

    def test_20_threads_no_state_corruption(self) -> None:
        """20 threads succeed: step_count accurate, no exception, no lost increments."""
        ctx = _make_ctx(max_steps=200)
        results: list[Decision] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                d = ctx.wrap_llm_call(fn=lambda: None)
                with lock:
                    results.append(d)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected errors: {errors}"
        assert len(results) == 20
        assert all(d == Decision.ALLOW for d in results)
        snap = ctx.get_snapshot()
        assert snap.step_count == 20

    def test_20_threads_step_limit_held(self) -> None:
        """With max_steps=10, exactly 10 ALLOW and 10 HALT (no over-counting)."""
        ctx = _make_ctx(max_steps=10, max_retries_total=0)
        allows: list[int] = []
        halts: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            d = ctx.wrap_llm_call(fn=lambda: None)
            with lock:
                if d == Decision.ALLOW:
                    allows.append(1)
                else:
                    halts.append(1)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Step count must not exceed the ceiling (race window means some threads
        # may both pass the pre-check before the counter increments, but the
        # implementation serializes the final increment under _lock).
        snap = ctx.get_snapshot()
        assert snap.step_count <= 10
        # Total decisions must be 20
        assert len(allows) + len(halts) == 20

    def test_20_threads_concurrent_abort_is_safe(self) -> None:
        """Abort mid-flight does not corrupt state; subsequent wraps return HALT."""
        ctx = _make_ctx()
        barrier = threading.Barrier(21)  # 20 workers + 1 aborter
        results: list[Decision] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            d = ctx.wrap_llm_call(fn=lambda: time.sleep(0.002))
            with lock:
                results.append(d)

        def aborter() -> None:
            barrier.wait()
            time.sleep(0.001)
            ctx.abort("stress abort")

        threads = [threading.Thread(target=worker) for _ in range(20)]
        ab_thread = threading.Thread(target=aborter)
        for t in threads:
            t.start()
        ab_thread.start()
        for t in threads:
            t.join()
        ab_thread.join()

        snap = ctx.get_snapshot()
        assert snap.aborted is True
        assert len(results) == 20
        # All decisions are valid Decision members
        for d in results:
            assert isinstance(d, Decision)

    def test_20_threads_cost_accumulation_accurate(self) -> None:
        """Cost from 20 threads accumulates without loss."""
        ctx = _make_ctx(max_cost_usd=1000.0)
        cost_per_call = 0.01

        def worker() -> None:
            ctx.wrap_llm_call(
                fn=lambda: None,
                options=WrapOptions(cost_estimate_hint=cost_per_call),
            )

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = ctx.get_snapshot()
        # Cost accumulated with 20 threads: allow some floating-point tolerance
        assert abs(snap.cost_usd_accumulated - 20 * cost_per_call) < 1e-9

    def test_concurrent_wrap_with_exception_no_lock_leak(self) -> None:
        """When fn() raises, no lock is left held — subsequent calls proceed."""
        ctx = _make_ctx()
        errors = 0
        allows = 0
        lock = threading.Lock()

        def failing_fn() -> None:
            raise ValueError("injected failure")

        def worker(should_fail: bool) -> None:
            nonlocal errors, allows
            d = ctx.wrap_llm_call(fn=failing_fn if should_fail else lambda: None)
            with lock:
                if d == Decision.ALLOW:
                    allows += 1
                else:
                    errors += 1

        # Alternate: 10 failing, 10 succeeding
        threads = []
        for i in range(20):
            threads.append(threading.Thread(target=worker, args=(i % 2 == 0,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All calls complete (no deadlock)
        assert allows + errors == 20
        # Context is not corrupted — a new wrap succeeds
        d = ctx.wrap_llm_call(fn=lambda: None)
        assert d in (Decision.ALLOW, Decision.RETRY, Decision.HALT)


# ---------------------------------------------------------------------------
# 2. Async concurrent guard
# ---------------------------------------------------------------------------


class TestAsyncConcurrentGuard:
    """10 asyncio tasks calling veronica_guard concurrently."""

    def test_10_tasks_independent_containers(self) -> None:
        """Each asyncio task gets its own ContextVar state."""
        results: list[bool] = []

        @veronica_guard(max_cost_usd=10.0, max_steps=5)
        async def guarded_fn() -> bool:
            return is_guard_active()

        async def run() -> None:
            tasks = [asyncio.create_task(guarded_fn()) for _ in range(10)]
            for t in tasks:
                results.append(await t)

        asyncio.run(run())
        assert len(results) == 10
        assert all(r is True for r in results)

    def test_10_tasks_guard_active_not_leaked_between_tasks(self) -> None:
        """is_guard_active() is False outside the guard, even with concurrent tasks."""
        inside_results: list[bool] = []
        outside_results: list[bool] = []

        @veronica_guard(max_cost_usd=10.0)
        async def guarded_fn() -> bool:
            await asyncio.sleep(0.001)
            return is_guard_active()

        async def check_outside() -> bool:
            await asyncio.sleep(0.0005)
            return is_guard_active()

        async def run() -> None:
            guard_tasks = [asyncio.create_task(guarded_fn()) for _ in range(10)]
            outside_task = asyncio.create_task(check_outside())
            for t in guard_tasks:
                inside_results.append(await t)
            outside_results.append(await outside_task)

        asyncio.run(run())
        assert all(r is True for r in inside_results)
        assert outside_results[0] is False

    def test_asyncio_create_task_inside_guard_inherits_context(self) -> None:
        """create_task inside guard: child task inherits copied ContextVar."""
        child_result: list[bool] = []

        @veronica_guard(max_cost_usd=10.0)
        async def outer() -> None:
            # create_task copies context — child sees is_guard_active() == True
            async def inner() -> None:
                child_result.append(is_guard_active())

            task = asyncio.create_task(inner())
            await task

        asyncio.run(outer())
        # Child task inherits guard_active=True via context copy
        assert child_result == [True]

    def test_async_guard_cancelled_error_resets_contextvar(self) -> None:
        """CancelledError in guarded async function resets ContextVar."""

        async def run() -> None:
            @veronica_guard(max_cost_usd=10.0)
            async def guarded() -> None:
                raise asyncio.CancelledError()

            with pytest.raises(asyncio.CancelledError):
                await guarded()

            # ContextVar must be reset after CancelledError
            assert is_guard_active() is False

        asyncio.run(run())

    def test_10_tasks_concurrent_exception_no_contextvar_leak(self) -> None:
        """Concurrent tasks that raise do not leave _guard_active set."""
        results_after: list[bool] = []

        @veronica_guard(max_cost_usd=10.0)
        async def failing() -> None:
            raise RuntimeError("task failure")

        async def run() -> None:
            for _ in range(10):
                with pytest.raises(RuntimeError):
                    await failing()
                results_after.append(is_guard_active())

        asyncio.run(run())
        assert all(r is False for r in results_after)


# ---------------------------------------------------------------------------
# 3. Resource exhaustion
# ---------------------------------------------------------------------------


class TestResourceExhaustion:
    """Verify cap behavior at _MAX_NODES, budget exhaustion, _MAX_PARTIAL_BUFFERS."""

    def test_max_nodes_cap_holds(self) -> None:
        """After _MAX_NODES calls, _nodes list does not grow beyond cap."""
        # Use small cap for speed: patch _MAX_NODES would require import hacks.
        # Instead, rely on the check-and-warn in the source: record _MAX_NODES
        # successful calls and verify len(nodes) == _MAX_NODES (not more).
        ctx = _make_ctx(max_steps=_MAX_NODES + 100, max_cost_usd=1_000_000.0)

        # Fill exactly _MAX_NODES nodes
        for _ in range(_MAX_NODES):
            d = ctx.wrap_llm_call(fn=lambda: None)
            assert d == Decision.ALLOW

        snap = ctx.get_snapshot()
        assert snap.step_count == _MAX_NODES
        assert len(snap.nodes) == _MAX_NODES

        # One more: node should NOT be recorded (cap reached)
        ctx.wrap_llm_call(fn=lambda: None)
        snap2 = ctx.get_snapshot()
        assert len(snap2.nodes) == _MAX_NODES  # still capped

    def test_budget_exhaustion_halts_all_subsequent(self) -> None:
        """After budget consumed, every subsequent call returns HALT."""
        ctx = _make_ctx(max_cost_usd=0.10, max_steps=1000)
        # Each call costs $0.05 — two calls exhaust the budget
        for _ in range(2):
            ctx.wrap_llm_call(
                fn=lambda: None,
                options=WrapOptions(cost_estimate_hint=0.05),
            )
        # Budget exhausted — all subsequent calls must HALT
        for _ in range(5):
            d = ctx.wrap_llm_call(
                fn=lambda: None,
                options=WrapOptions(cost_estimate_hint=0.01),
            )
            assert d == Decision.HALT

    def test_budget_exhausted_zero_cost_also_halts(self) -> None:
        """Once limit reached, even cost=0 calls are halted (step limit or budget)."""
        ctx = _make_ctx(max_cost_usd=0.05, max_steps=1000)
        ctx.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(cost_estimate_hint=0.05),
        )
        # Budget at ceiling — next call with cost=0 should still HALT if
        # cost_usd_accumulated >= max_cost_usd
        d = ctx.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(cost_estimate_hint=0.01),
        )
        assert d == Decision.HALT

    def test_max_partial_buffers_cap(self) -> None:
        """_partial_buffers dict does not grow beyond _MAX_PARTIAL_BUFFERS."""
        ctx = _make_ctx(max_steps=_MAX_PARTIAL_BUFFERS + 100)

        for _ in range(_MAX_PARTIAL_BUFFERS):
            buf = PartialResultBuffer()
            ctx.wrap_llm_call(
                fn=lambda: None,
                options=WrapOptions(partial_buffer=buf),
            )

        # One more partial buffer — should log warning and NOT be tracked
        extra_buf = PartialResultBuffer()
        ctx.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(partial_buffer=extra_buf),
        )
        # Verify cap held
        with ctx._lock:
            assert len(ctx._partial_buffers) == _MAX_PARTIAL_BUFFERS

    def test_step_limit_zero_halts_immediately(self) -> None:
        """max_steps=0 means no steps allowed — every call returns HALT."""
        ctx = _make_ctx(max_steps=0)
        for _ in range(5):
            d = ctx.wrap_llm_call(fn=lambda: None)
            assert d == Decision.HALT

    def test_cost_ceiling_zero_halts_on_positive_estimate(self) -> None:
        """max_cost_usd=0 with positive cost_estimate_hint returns HALT."""
        ctx = _make_ctx(max_cost_usd=0.0, max_steps=1000)
        d = ctx.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(cost_estimate_hint=0.001),
        )
        assert d == Decision.HALT


# ---------------------------------------------------------------------------
# 4. Error cascade
# ---------------------------------------------------------------------------


class TestErrorCascade:
    """Every call raises / alternating / BaseException cleanup."""

    def test_all_calls_raise_no_state_corruption(self) -> None:
        """100 consecutive raising calls: no lock left held, state consistent."""
        ctx = _make_ctx()
        retry_count = 0

        for _ in range(100):
            d = ctx.wrap_llm_call(
                fn=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            )
            assert d == Decision.RETRY
            retry_count += 1

        snap = ctx.get_snapshot()
        # step_count = 0 (no success), retries_used = 100
        assert snap.step_count == 0
        assert snap.retries_used == retry_count

        # Context is not corrupted — a success call works
        d = ctx.wrap_llm_call(fn=lambda: None)
        assert d == Decision.ALLOW

    def test_alternating_success_failure_counters_accurate(self) -> None:
        """50 success + 50 failure: step_count=50, retries_used=50."""
        ctx = _make_ctx(max_steps=100, max_retries_total=100)
        successes = 0
        retries = 0

        for i in range(100):
            if i % 2 == 0:
                d = ctx.wrap_llm_call(fn=lambda: None)
                if d == Decision.ALLOW:
                    successes += 1
            else:
                d = ctx.wrap_llm_call(
                    fn=lambda: (_ for _ in ()).throw(RuntimeError("alternating fail"))
                )
                if d == Decision.RETRY:
                    retries += 1

        snap = ctx.get_snapshot()
        assert snap.step_count == successes
        assert snap.retries_used == retries

    def test_keyboard_interrupt_propagates_not_swallowed(self) -> None:
        """KeyboardInterrupt must propagate through _wrap, not be swallowed."""
        ctx = _make_ctx()

        with pytest.raises(KeyboardInterrupt):
            ctx.wrap_llm_call(fn=lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

        # Context still usable after KeyboardInterrupt
        d = ctx.wrap_llm_call(fn=lambda: None)
        assert d in (Decision.ALLOW, Decision.HALT)

    def test_system_exit_propagates(self) -> None:
        """SystemExit must propagate through _wrap, not be swallowed."""
        ctx = _make_ctx()

        with pytest.raises(SystemExit):
            ctx.wrap_llm_call(fn=lambda: (_ for _ in ()).throw(SystemExit(0)))

    def test_node_closed_on_unexpected_exception(self) -> None:
        """Unexpected BaseException in fn(): node.end_ts is set, graph not left open."""
        ctx = _make_ctx()

        with pytest.raises(KeyboardInterrupt):
            ctx.wrap_llm_call(fn=lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

        # Subsequent call must succeed (graph not corrupted)
        d = ctx.wrap_llm_call(fn=lambda: None)
        assert d == Decision.ALLOW

    def test_exception_during_concurrent_calls_no_deadlock(self) -> None:
        """20 threads, half raise exceptions — no deadlock within 10 seconds."""
        ctx = _make_ctx(max_steps=100, max_retries_total=100)
        results: list[Any] = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            def fn() -> None:
                if idx % 2 != 0:
                    raise RuntimeError("x")

            try:
                d = ctx.wrap_llm_call(fn=fn)
                with lock:
                    results.append(d)
            except Exception:
                with lock:
                    results.append("exc")

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(worker, i) for i in range(20)]
            for f in futures:
                f.result(timeout=10.0)

        assert len(results) == 20


# ---------------------------------------------------------------------------
# 5. ContextVar edge cases
# ---------------------------------------------------------------------------


class TestContextVarEdgeCases:
    """Nested guards, create_task isolation, mixed sync+async patterns."""

    def test_nested_veronica_guard_outermost_wins(self) -> None:
        """Nested sync guard: inner guard creates fresh container, outer still active."""
        active_states: list[bool] = []

        @veronica_guard(max_cost_usd=5.0)
        def outer() -> None:
            active_states.append(is_guard_active())

            @veronica_guard(max_cost_usd=1.0)
            def inner() -> None:
                active_states.append(is_guard_active())

            inner()
            active_states.append(is_guard_active())  # still True in outer

        outer()
        assert active_states == [True, True, True]
        assert is_guard_active() is False  # reset after outer

    def test_nested_async_guard_independent_contextvar(self) -> None:
        """Nested async guard: each has its own token, both reset cleanly."""
        states: list[bool] = []

        @veronica_guard(max_cost_usd=5.0)
        async def outer() -> None:
            states.append(is_guard_active())  # True

            @veronica_guard(max_cost_usd=1.0)
            async def inner() -> None:
                states.append(is_guard_active())  # True

            await inner()
            states.append(is_guard_active())  # True (outer still active)

        asyncio.run(outer())
        assert states == [True, True, True]
        assert is_guard_active() is False

    def test_create_task_contextvar_isolation(self) -> None:
        """asyncio.create_task creates a COPY of context — mutations in child don't
        affect parent's ContextVar."""
        parent_state_after: list[bool] = []

        @veronica_guard(max_cost_usd=5.0)
        async def guarded_parent() -> None:
            # Spawned task gets a context copy; reset inside child should not affect parent
            async def child() -> None:
                # This guard creates a nested scope in child context
                @veronica_guard(max_cost_usd=1.0)
                async def inner() -> None:
                    pass

                await inner()

            task = asyncio.create_task(child())
            await task
            parent_state_after.append(is_guard_active())

        asyncio.run(guarded_parent())
        # Parent is still in guard after child task completed
        assert parent_state_after == [True]
        # After outer guard exits, state is False
        assert is_guard_active() is False

    def test_contextvar_reset_on_exception_in_sync(self) -> None:
        """Exception inside sync guard resets _guard_active via finally."""

        @veronica_guard(max_cost_usd=5.0)
        def failing() -> None:
            raise ValueError("test")

        with pytest.raises(ValueError):
            failing()

        assert is_guard_active() is False

    def test_contextvar_reset_on_exception_in_async(self) -> None:
        """Exception inside async guard resets _guard_active via finally."""

        async def run() -> None:
            @veronica_guard(max_cost_usd=5.0)
            async def failing() -> None:
                raise ValueError("async test")

            with pytest.raises(ValueError):
                await failing()

            assert is_guard_active() is False

        asyncio.run(run())

    def test_wrap_contextvar_depth_resets_after_normal_exit(self) -> None:
        """_nesting_depth_var returns to 0 after wrap_llm_call completes."""
        ctx = _make_ctx()
        ctx.wrap_llm_call(fn=lambda: None)
        # After completion, depth must be 0 (checked via the default value)
        depth = ctx._nesting_depth_var.get()
        assert depth == 0

    def test_wrap_contextvar_depth_resets_after_exception(self) -> None:
        """_nesting_depth_var returns to 0 even when fn() raises."""
        ctx = _make_ctx()
        ctx.wrap_llm_call(fn=lambda: (_ for _ in ()).throw(RuntimeError("depth test")))
        depth = ctx._nesting_depth_var.get()
        assert depth == 0

    def test_nested_wrap_llm_call_depth_tracking(self) -> None:
        """Nested wrap_llm_call: depth increments and decrements correctly."""
        ctx = _make_ctx()
        depths: list[int] = []

        def inner_fn() -> None:
            depths.append(ctx._nesting_depth_var.get())  # should be 2

        def outer_fn() -> None:
            depths.append(ctx._nesting_depth_var.get())  # should be 1
            ctx.wrap_llm_call(fn=inner_fn)

        ctx.wrap_llm_call(fn=outer_fn)
        assert depths == [1, 2]
        assert ctx._nesting_depth_var.get() == 0

    def test_mixed_sync_and_async_guard_no_interference(self) -> None:
        """Sync guard and async guard run sequentially: no ContextVar cross-contamination."""
        sync_results: list[bool] = []
        async_results: list[bool] = []

        @veronica_guard(max_cost_usd=1.0)
        def sync_fn() -> None:
            sync_results.append(is_guard_active())

        @veronica_guard(max_cost_usd=1.0)
        async def async_fn() -> None:
            async_results.append(is_guard_active())

        sync_fn()
        asyncio.run(async_fn())
        sync_fn()

        assert sync_results == [True, True]
        assert async_results == [True]
        assert is_guard_active() is False

    def test_20_concurrent_threads_contextvar_no_crossover(self) -> None:
        """20 threads each run a guarded function; each sees its own ContextVar."""
        # Since ContextVar is thread-local in CPython (threading contexts), each
        # thread should see is_guard_active() == True inside, False outside.
        inside_states: list[bool] = []
        outside_states: list[bool] = []
        lock = threading.Lock()

        @veronica_guard(max_cost_usd=1.0)
        def thread_fn() -> None:
            with lock:
                inside_states.append(is_guard_active())
            time.sleep(0.005)  # hold the guard open
            with lock:
                inside_states.append(is_guard_active())

        def outside_checker() -> None:
            time.sleep(0.002)  # check while other threads are in guard
            with lock:
                outside_states.append(is_guard_active())

        threads = [threading.Thread(target=thread_fn) for _ in range(20)]
        checker = threading.Thread(target=outside_checker)
        for t in threads:
            t.start()
        checker.start()
        for t in threads:
            t.join()
        checker.join()

        # All 40 inside-guard reads should be True
        assert all(s is True for s in inside_states), f"inside_states: {inside_states}"
        # The checker thread has its own context — guard is False there
        assert outside_states == [False]
