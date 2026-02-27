"""Performance benchmarks for DistributedCircuitBreaker.

Measures actual latency of each operation against fakeredis to establish
baseline overhead. Real Redis will add network RTT on top of these numbers.

Run:
    pytest tests/bench_distributed_circuit_breaker.py -v -s
    # Or standalone:
    python tests/bench_distributed_circuit_breaker.py
"""

from __future__ import annotations

import statistics
import time
from typing import Callable, List

import pytest

try:
    import fakeredis
except ImportError:
    fakeredis = None

from veronica_core.circuit_breaker import CircuitState
from veronica_core.distributed import DistributedCircuitBreaker
from veronica_core.runtime_policy import PolicyContext

pytestmark = pytest.mark.skipif(fakeredis is None, reason="fakeredis required")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CTX = PolicyContext()


def _make_dcb(**kwargs) -> DistributedCircuitBreaker:
    """Create a DistributedCircuitBreaker backed by fakeredis."""
    fake_server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeRedis(server=fake_server, decode_responses=True)

    defaults = {
        "redis_url": "redis://localhost:6379",
        "circuit_id": "bench",
        "failure_threshold": 5,
        "recovery_timeout": 60.0,
        "ttl_seconds": 3600,
        "half_open_slot_timeout": 120.0,
    }
    defaults.update(kwargs)

    dcb = DistributedCircuitBreaker(**defaults)
    # Inject fakeredis client
    dcb._client = fake_client
    dcb._using_fallback = False
    dcb._owns_client = False
    dcb._register_scripts()
    return dcb


def _bench(
    func: Callable,
    iterations: int = 1000,
    warmup: int = 50,
    label: str = "",
) -> dict:
    """Run a callable `iterations` times and collect timing stats."""
    # Warmup
    for _ in range(warmup):
        func()

    latencies: List[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        func()
        t1 = time.perf_counter_ns()
        latencies.append((t1 - t0) / 1_000)  # microseconds

    result = {
        "label": label,
        "iterations": iterations,
        "mean_us": statistics.mean(latencies),
        "median_us": statistics.median(latencies),
        "p95_us": sorted(latencies)[int(0.95 * len(latencies))],
        "p99_us": sorted(latencies)[int(0.99 * len(latencies))],
        "min_us": min(latencies),
        "max_us": max(latencies),
        "stdev_us": statistics.stdev(latencies) if len(latencies) > 1 else 0,
    }
    return result


def _print_result(r: dict) -> None:
    print(
        f"  {r['label']:40s} | "
        f"mean={r['mean_us']:8.1f}us | "
        f"median={r['median_us']:8.1f}us | "
        f"p95={r['p95_us']:8.1f}us | "
        f"p99={r['p99_us']:8.1f}us | "
        f"min={r['min_us']:8.1f}us | "
        f"max={r['max_us']:8.1f}us"
    )


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


class TestBenchCheck:
    """Benchmark check() in each circuit state."""

    def test_check_closed(self) -> None:
        dcb = _make_dcb()
        r = _bench(lambda: dcb.check(_CTX), label="check() [CLOSED]")
        _print_result(r)
        # fakeredis: should be well under 1ms
        assert r["p99_us"] < 5000, f"check() p99 too slow: {r['p99_us']}us"

    def test_check_open(self) -> None:
        dcb = _make_dcb(failure_threshold=2, recovery_timeout=600.0)
        dcb.record_failure()
        dcb.record_failure()
        assert dcb.state == CircuitState.OPEN
        r = _bench(lambda: dcb.check(_CTX), label="check() [OPEN]")
        _print_result(r)
        assert r["p99_us"] < 5000

    def test_check_half_open_denied(self) -> None:
        dcb = _make_dcb(failure_threshold=2, recovery_timeout=0.0)
        dcb.record_failure()
        dcb.record_failure()
        # First check claims the slot
        dcb.check(_CTX)
        # Subsequent checks should be denied (slot taken)
        r = _bench(lambda: dcb.check(_CTX), label="check() [HALF_OPEN denied]")
        _print_result(r)
        assert r["p99_us"] < 5000


class TestBenchRecordOps:
    """Benchmark record_success() and record_failure()."""

    def test_record_success(self) -> None:
        dcb = _make_dcb()
        r = _bench(lambda: dcb.record_success(), label="record_success()")
        _print_result(r)
        assert r["p99_us"] < 5000

    def test_record_failure(self) -> None:
        dcb = _make_dcb(failure_threshold=999999)
        r = _bench(lambda: dcb.record_failure(), label="record_failure()")
        _print_result(r)
        assert r["p99_us"] < 5000


class TestBenchSnapshot:
    """Benchmark snapshot() vs individual property reads."""

    def test_snapshot_single_read(self) -> None:
        dcb = _make_dcb()
        # Pre-populate some state
        for _ in range(3):
            dcb.record_failure()
        dcb.record_success()

        r = _bench(lambda: dcb.snapshot(), label="snapshot() [single HGETALL]")
        _print_result(r)
        assert r["p99_us"] < 5000

    def test_individual_properties_n_plus_1(self) -> None:
        dcb = _make_dcb()
        for _ in range(3):
            dcb.record_failure()
        dcb.record_success()

        def read_all_props():
            _ = dcb.state
            _ = dcb.failure_count
            _ = dcb.success_count

        r = _bench(read_all_props, label="state+failure+success [3 reads]")
        _print_result(r)

    def test_snapshot_vs_properties_ratio(self) -> None:
        """Snapshot should be faster than 3 individual reads."""
        dcb = _make_dcb()
        for _ in range(3):
            dcb.record_failure()
        dcb.record_success()

        r_snap = _bench(lambda: dcb.snapshot(), label="snapshot()")
        r_props = _bench(
            lambda: (dcb.state, dcb.failure_count, dcb.success_count),
            label="3x property reads",
        )
        _print_result(r_snap)
        _print_result(r_props)

        ratio = r_props["mean_us"] / r_snap["mean_us"]
        print(f"\n  Speedup: snapshot is {ratio:.2f}x faster than 3 property reads")
        # snapshot (1 HGETALL) should be faster than 3 separate reads
        assert ratio > 1.0, "snapshot should be faster than N+1 reads"


class TestBenchFullCycle:
    """Benchmark a complete check -> call -> record cycle."""

    def test_full_success_cycle(self) -> None:
        dcb = _make_dcb()

        def cycle():
            d = dcb.check(_CTX)
            if d.allowed:
                dcb.record_success()

        r = _bench(cycle, label="check+record_success [full cycle]")
        _print_result(r)
        assert r["p99_us"] < 10000  # 10ms ceiling for full cycle

    def test_full_failure_cycle(self) -> None:
        dcb = _make_dcb(failure_threshold=999999)

        def cycle():
            d = dcb.check(_CTX)
            if d.allowed:
                dcb.record_failure()

        r = _bench(cycle, label="check+record_failure [full cycle]")
        _print_result(r)
        assert r["p99_us"] < 10000


class TestBenchLocalFallback:
    """Compare distributed vs local fallback performance."""

    def test_local_fallback_check(self) -> None:
        dcb = _make_dcb()
        dcb._using_fallback = True

        r = _bench(lambda: dcb.check(_CTX), label="check() [LOCAL fallback]")
        _print_result(r)

    def test_distributed_vs_local_overhead(self) -> None:
        """Measure the overhead of Redis vs local."""
        dcb_redis = _make_dcb()
        dcb_local = _make_dcb()
        dcb_local._using_fallback = True

        r_redis = _bench(lambda: dcb_redis.check(_CTX), label="check [Redis]")
        r_local = _bench(lambda: dcb_local.check(_CTX), label="check [Local]")
        _print_result(r_redis)
        _print_result(r_local)

        overhead = r_redis["mean_us"] / r_local["mean_us"]
        print(f"\n  Redis overhead: {overhead:.2f}x vs local")
        print(f"  Redis mean: {r_redis['mean_us']:.1f}us, Local mean: {r_local['mean_us']:.1f}us")
        print(f"  Absolute overhead: {r_redis['mean_us'] - r_local['mean_us']:.1f}us per call")

        # For context: LLM calls are 200ms-30s
        llm_min_ms = 200
        overhead_pct = (r_redis["mean_us"] / 1000) / llm_min_ms * 100
        print(f"  As % of fastest LLM call ({llm_min_ms}ms): {overhead_pct:.3f}%")


class TestBenchReset:
    """Benchmark reset()."""

    def test_reset(self) -> None:
        dcb = _make_dcb()
        r = _bench(lambda: dcb.reset(), label="reset()")
        _print_result(r)
        assert r["p99_us"] < 5000


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

def _run_all_benchmarks() -> None:
    """Run all benchmarks and print summary table."""
    print("\n" + "=" * 100)
    print("DistributedCircuitBreaker Performance Benchmarks (fakeredis)")
    print("=" * 100)
    print("  Iterations: 1000, Warmup: 50\n")

    results: List[dict] = []

    # CLOSED check
    dcb = _make_dcb()
    results.append(_bench(lambda: dcb.check(_CTX), label="check() [CLOSED]"))

    # OPEN check
    dcb2 = _make_dcb(failure_threshold=2, recovery_timeout=600.0)
    dcb2.record_failure()
    dcb2.record_failure()
    results.append(_bench(lambda: dcb2.check(_CTX), label="check() [OPEN]"))

    # record_success
    dcb3 = _make_dcb()
    results.append(_bench(lambda: dcb3.record_success(), label="record_success()"))

    # record_failure
    dcb4 = _make_dcb(failure_threshold=999999)
    results.append(_bench(lambda: dcb4.record_failure(), label="record_failure()"))

    # snapshot
    dcb5 = _make_dcb()
    dcb5.record_failure()
    dcb5.record_failure()
    dcb5.record_success()
    results.append(_bench(lambda: dcb5.snapshot(), label="snapshot()"))

    # 3x property reads
    results.append(
        _bench(
            lambda: (dcb5.state, dcb5.failure_count, dcb5.success_count),
            label="state+failure+success [3 reads]",
        )
    )

    # full cycle
    dcb6 = _make_dcb()

    def success_cycle():
        d = dcb6.check(_CTX)
        if d.allowed:
            dcb6.record_success()

    results.append(_bench(success_cycle, label="check+record_success [cycle]"))

    # reset
    dcb7 = _make_dcb()
    results.append(_bench(lambda: dcb7.reset(), label="reset()"))

    # local fallback
    dcb8 = _make_dcb()
    dcb8._using_fallback = True
    results.append(_bench(lambda: dcb8.check(_CTX), label="check() [LOCAL fallback]"))

    print("\n  Results:")
    print(f"  {'Operation':40s} | {'mean':>10s} | {'median':>10s} | {'p95':>10s} | {'p99':>10s}")
    print(f"  {'-'*40}-+-{'-'*10}-+-{'-'*10}-+-{'-'*10}-+-{'-'*10}")
    for r in results:
        print(
            f"  {r['label']:40s} | "
            f"{r['mean_us']:8.1f}us | "
            f"{r['median_us']:8.1f}us | "
            f"{r['p95_us']:8.1f}us | "
            f"{r['p99_us']:8.1f}us"
        )

    # Summary
    redis_mean = results[0]["mean_us"]  # check CLOSED
    local_mean = results[-1]["mean_us"]  # local fallback
    snapshot_mean = results[4]["mean_us"]
    props_mean = results[5]["mean_us"]

    print("\n  Summary:")
    print(f"    Redis overhead vs local: {redis_mean / local_mean:.2f}x ({redis_mean - local_mean:.1f}us)")
    print(f"    snapshot() vs 3 reads:   {props_mean / snapshot_mean:.2f}x faster ({props_mean - snapshot_mean:.1f}us saved)")
    print(f"    As % of LLM call (200ms): {redis_mean / 1000 / 200 * 100:.4f}%")
    print(f"    As % of LLM call (2000ms): {redis_mean / 1000 / 2000 * 100:.5f}%")
    print()


if __name__ == "__main__":
    _run_all_benchmarks()
