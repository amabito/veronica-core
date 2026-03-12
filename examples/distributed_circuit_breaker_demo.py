"""Distributed circuit breaker demo: 3 agents sharing Redis-backed circuit state.

Shows how DistributedCircuitBreaker coordinates failure isolation across
independent processes using a shared Redis key. Uses fakeredis so the demo
runs without a real Redis instance.

Scenario:
  - Agent A (caller A) triggers 3 consecutive failures.
  - After the third failure the circuit opens in Redis.
  - Agent B (caller B) queries the same circuit and receives DENIED.
  - Agent C (caller C) observes the circuit state and waits.
  - After simulated recovery timeout, one caller claims the HALF_OPEN slot.

Run:
    python examples/distributed_circuit_breaker_demo.py
"""

from __future__ import annotations

import threading
import time

import fakeredis

from veronica_core.circuit_breaker import CircuitState
from veronica_core.distributed import DistributedCircuitBreaker
from veronica_core.runtime_policy import PolicyContext


# ---------------------------------------------------------------------------
# Helper: create a DistributedCircuitBreaker backed by a shared fakeredis server
# ---------------------------------------------------------------------------


def _make_breaker(
    shared_client: fakeredis.FakeRedis,
    circuit_id: str = "demo-service",
    failure_threshold: int = 3,
    recovery_timeout: float = 2.0,  # short timeout so the demo finishes quickly
    half_open_slot_timeout: float = 5.0,
) -> DistributedCircuitBreaker:
    """Build a DistributedCircuitBreaker that reuses an existing fakeredis client.

    In production, pass ``redis_client`` to share a connection pool across
    multiple breakers that protect the same service.
    """
    dcb = DistributedCircuitBreaker.__new__(DistributedCircuitBreaker)
    dcb._redis_url = "redis://fake"
    dcb._circuit_id = circuit_id
    dcb._key = f"veronica:circuit:{circuit_id}"
    dcb._failure_threshold = failure_threshold
    dcb._recovery_timeout = recovery_timeout
    dcb._ttl = 3600
    dcb._fallback_on_error = True
    dcb._half_open_slot_timeout = half_open_slot_timeout
    from veronica_core.circuit_breaker import CircuitBreaker
    dcb._fallback = CircuitBreaker(
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout,
    )
    dcb._using_fallback = False
    dcb._client = shared_client
    dcb._owns_client = False
    dcb._lock = threading.Lock()
    dcb._last_reconnect_attempt = 0.0

    # Register Lua scripts on the shared client
    import veronica_core.distributed as _dist
    dcb._script_failure = shared_client.register_script(_dist._LUA_RECORD_FAILURE)
    dcb._script_success = shared_client.register_script(_dist._LUA_RECORD_SUCCESS)
    dcb._script_check = shared_client.register_script(_dist._LUA_CHECK)
    return dcb


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def run_demo() -> None:
    # One fakeredis server simulates the shared Redis instance.
    server = fakeredis.FakeServer()

    # Three independent callers (simulating separate processes) each get their
    # own DistributedCircuitBreaker that points at the same circuit_id key.
    client_a = fakeredis.FakeRedis(server=server, decode_responses=True)
    client_b = fakeredis.FakeRedis(server=server, decode_responses=True)
    client_c = fakeredis.FakeRedis(server=server, decode_responses=True)

    breaker_a = _make_breaker(client_a, circuit_id="demo-llm-service")
    breaker_b = _make_breaker(client_b, circuit_id="demo-llm-service")
    breaker_c = _make_breaker(client_c, circuit_id="demo-llm-service")

    ctx = PolicyContext()

    # ------------------------------------------------------------------
    # Step 1: All agents start with a CLOSED circuit
    # ------------------------------------------------------------------
    print("Step 1 -- Initial state")
    for label, breaker in (("A", breaker_a), ("B", breaker_b), ("C", breaker_c)):
        assert breaker.state == CircuitState.CLOSED
        print(f"  Agent {label}: state={breaker.state.value}")

    # ------------------------------------------------------------------
    # Step 2: Agent A simulates 3 consecutive backend failures
    # ------------------------------------------------------------------
    print("\nStep 2 -- Agent A records 3 failures")
    for i in range(1, 4):
        breaker_a.record_failure()
        state_after = breaker_a.state
        print(f"  Agent A failure #{i}: circuit state={state_after.value}")

    assert breaker_a.state == CircuitState.OPEN, "circuit must be OPEN after threshold"

    # ------------------------------------------------------------------
    # Step 3: Agent B queries the same circuit and is denied
    # ------------------------------------------------------------------
    print("\nStep 3 -- Agent B checks circuit (should be denied)")
    decision_b = breaker_b.check(ctx)
    print(f"  Agent B: allowed={decision_b.allowed}, reason={decision_b.reason!r}")
    assert not decision_b.allowed, "Agent B must be denied when circuit is OPEN"

    # ------------------------------------------------------------------
    # Step 4: Agent C also observes the OPEN state
    # ------------------------------------------------------------------
    print("\nStep 4 -- Agent C observes state (read-only)")
    snapshot_c = breaker_c.snapshot()
    print(
        f"  Agent C snapshot: state={snapshot_c.state.value}, "
        f"failure_count={snapshot_c.failure_count}, "
        f"distributed={snapshot_c.distributed}"
    )
    assert snapshot_c.state == CircuitState.OPEN

    # ------------------------------------------------------------------
    # Step 5: Wait for recovery timeout, then one caller claims HALF_OPEN
    # ------------------------------------------------------------------
    print(f"\nStep 5 -- Waiting {breaker_a._recovery_timeout}s for recovery timeout...")
    time.sleep(breaker_a._recovery_timeout + 0.1)

    # Agent B is the first to check after timeout -- it claims the HALF_OPEN slot.
    decision_b2 = breaker_b.check(ctx)
    print(
        f"  Agent B after timeout: allowed={decision_b2.allowed}, "
        f"state={breaker_b.state.value}"
    )
    assert decision_b2.allowed, "Agent B must be allowed to probe in HALF_OPEN"
    assert breaker_b.state == CircuitState.HALF_OPEN

    # Agent C tries immediately after -- slot is already taken.
    decision_c = breaker_c.check(ctx)
    print(
        f"  Agent C (slot taken): allowed={decision_c.allowed}, "
        f"state={breaker_c.state.value}"
    )
    assert not decision_c.allowed, "Agent C must be denied -- HALF_OPEN slot is taken"

    # ------------------------------------------------------------------
    # Step 6: Agent B's probe succeeds → circuit closes for everyone
    # ------------------------------------------------------------------
    print("\nStep 6 -- Agent B probe succeeds, circuit closes")
    breaker_b.record_success()

    for label, breaker in (("A", breaker_a), ("B", breaker_b), ("C", breaker_c)):
        state = breaker.state
        print(f"  Agent {label}: state={state.value}")
        assert state == CircuitState.CLOSED, f"Agent {label} should see CLOSED after recovery"

    print("\n[PASS] All assertions passed.")


if __name__ == "__main__":
    run_demo()
