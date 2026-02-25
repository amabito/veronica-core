"""CrewAI + VERONICA Core — Circuit Breaker & Safe Mode via VeronicaIntegration.

Shows how VeronicaIntegration wraps a CrewAI crew.kickoff() call to enforce:
  - circuit breaker: auto-halt after consecutive crew failures
  - SAFE_MODE: hard stop that blocks all kickoffs before they start
  - manual reset: clear circuit and resume normal operation

Requirements:
    No extra packages needed — demo uses a stub Crew.
    (crewai is NOT required — stub simulates success/failure)
"""

from __future__ import annotations

import sys
from typing import Any

from veronica_core import (
    CircuitBreaker,
    CircuitState,
    MemoryBackend,
    VeronicaIntegration,
    VeronicaState,
)

# ---------------------------------------------------------------------------
# Minimal stub Crew — simulates CrewAI without a real API key
# ---------------------------------------------------------------------------

RESULT_SUCCESS = "success"
RESULT_FAILURE = "failure"


class _StubCrew:
    """Fake CrewAI Crew that cycles through a preset sequence of outcomes.

    Pass a list of True/False values; each kickoff() call consumes the next
    entry.  True == success, False == failure (raises RuntimeError).
    """

    def __init__(self, outcomes: list[bool], name: str = "StubCrew") -> None:
        self._outcomes = list(outcomes)
        self._call_index = 0
        self.name = name

    def kickoff(self, inputs: dict[str, Any]) -> str:
        """Simulate a crew run.

        Args:
            inputs: Arbitrary crew inputs (ignored by the stub).

        Returns:
            A short result string on success.

        Raises:
            RuntimeError: On simulated failure.
        """
        idx = self._call_index
        self._call_index += 1

        if idx >= len(self._outcomes):
            # Default to failure once the preset list is exhausted
            outcome = False
        else:
            outcome = self._outcomes[idx]

        if outcome:
            return f"[{self.name}] run #{idx + 1}: task completed successfully"
        else:
            raise RuntimeError(f"[{self.name}] run #{idx + 1}: crew task failed")


# ---------------------------------------------------------------------------
# VERONICA-guarded crew runner
# ---------------------------------------------------------------------------

CREW_ENTITY = "research_crew"


def run_crew(
    crew: _StubCrew,
    veronica: VeronicaIntegration,
    breaker: CircuitBreaker,
    inputs: dict[str, Any],
) -> str | None:
    """Execute crew.kickoff() with VERONICA protection.

    Checks:
    1. SAFE_MODE — hard halt, crew never starts.
    2. Circuit breaker OPEN — halt until recovery timeout elapses.
    3. Cooldown — per-entity backoff after repeated failures.

    Returns the crew result on success, or None if blocked.
    """
    # Check 1: global SAFE_MODE
    if veronica.state.current_state == VeronicaState.SAFE_MODE:
        print("  [VERONICA] BLOCKED - system is in SAFE_MODE. Reset required.")
        return None

    # Check 2: circuit breaker
    from veronica_core.runtime_policy import PolicyContext

    decision = breaker.check(PolicyContext())
    if not decision.allowed:
        print(f"  [VERONICA] BLOCKED - circuit {breaker.state.value}: {decision.reason}")
        return None

    # Check 3: per-entity cooldown
    if veronica.is_in_cooldown(CREW_ENTITY):
        remaining = veronica.get_cooldown_remaining(CREW_ENTITY)
        print(f"  [VERONICA] BLOCKED - {CREW_ENTITY} in cooldown ({remaining:.1f}s remaining)")
        return None

    # Run the crew
    try:
        result = crew.kickoff(inputs)
        veronica.record_pass(CREW_ENTITY)
        breaker.record_success()
        print(f"  [OK] {result}")
        return result
    except RuntimeError as exc:
        print(f"  [FAIL] {exc}")
        breaker.record_failure()
        cooldown_activated = veronica.record_fail(CREW_ENTITY)
        if cooldown_activated:
            remaining = veronica.get_cooldown_remaining(CREW_ENTITY)
            print(
                f"  [VERONICA] Cooldown activated for {CREW_ENTITY} "
                f"({remaining:.1f}s)"
            )
        return None


# ---------------------------------------------------------------------------
# Demo 1: circuit breaker trips after consecutive crew failures
# ---------------------------------------------------------------------------

def demo_circuit_breaker() -> None:
    print("=" * 60)
    print("Demo 1: Circuit Breaker (threshold=3 failures)")
    print("=" * 60)

    veronica = VeronicaIntegration(
        cooldown_fails=5,       # high threshold so cooldown doesn't fire first
        cooldown_seconds=60,
        auto_save_interval=0,   # no disk writes in demo
        backend=MemoryBackend(),
    )

    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)

    # 2 successes, then 3 consecutive failures to open the circuit, then one more attempt
    crew = _StubCrew(
        outcomes=[True, True, False, False, False, True],
        name="ResearchCrew",
    )
    inputs = {"topic": "AI safety"}

    for attempt in range(1, 7):
        print(f"\nAttempt {attempt}:")
        run_crew(crew, veronica, breaker, inputs)
        print(
            f"  Circuit state: {breaker.state.value} "
            f"(failures: {breaker.failure_count})"
        )

    print(f"\nFinal circuit state: {breaker.state.value}")
    print("  -> Circuit opened after 3 consecutive failures.")
    print("     Attempt 6 was blocked without calling kickoff().")


# ---------------------------------------------------------------------------
# Demo 2: SAFE_MODE halt blocks all kickoffs before they start
# ---------------------------------------------------------------------------

def demo_safe_mode_halt() -> None:
    print("\n" + "=" * 60)
    print("Demo 2: SAFE_MODE Halt")
    print("=" * 60)

    veronica = VeronicaIntegration(
        cooldown_fails=3,
        cooldown_seconds=60,
        auto_save_interval=0,
        backend=MemoryBackend(),
    )
    breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)

    # Crew would succeed, but SAFE_MODE prevents it from running
    crew = _StubCrew(outcomes=[True, True, True], name="AnalysisCrew")
    inputs = {"query": "market trends"}

    print("\nNormal run (before SAFE_MODE):")
    run_crew(crew, veronica, breaker, inputs)

    # Activate SAFE_MODE — simulates operator emergency stop
    veronica.state.transition(VeronicaState.SAFE_MODE, "operator emergency stop")
    print(f"\nSAFE_MODE activated. State: {veronica.state.current_state.value}")

    print("\nAttempts while in SAFE_MODE:")
    for attempt in range(1, 4):
        print(f"\n  Attempt {attempt}:")
        run_crew(crew, veronica, breaker, inputs)

    print("\n  -> All kickoffs blocked; crew.kickoff() was never called.")


# ---------------------------------------------------------------------------
# Demo 3: manual reset resumes execution
# ---------------------------------------------------------------------------

def demo_manual_reset() -> None:
    print("\n" + "=" * 60)
    print("Demo 3: Manual Reset Resumes Execution")
    print("=" * 60)

    veronica = VeronicaIntegration(
        cooldown_fails=3,
        cooldown_seconds=60,
        auto_save_interval=0,
        backend=MemoryBackend(),
    )
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

    # 2 failures open the circuit, then we reset and run again
    crew = _StubCrew(
        outcomes=[False, False, True],
        name="WritingCrew",
    )
    inputs = {"task": "write report"}

    print("\nRuns that open the circuit:")
    for attempt in range(1, 3):
        print(f"\n  Attempt {attempt}:")
        run_crew(crew, veronica, breaker, inputs)

    print(f"\n  Circuit state: {breaker.state.value}")
    print("  -> Circuit is OPEN; further attempts would be blocked.")

    # Operator resets the circuit breaker manually
    print("\nOperator action: reset circuit breaker")
    breaker.reset()
    print(f"  Circuit state after reset: {breaker.state.value}")

    # Also clear per-entity cooldown state if activated
    veronica.state.fail_counts.pop(CREW_ENTITY, None)
    veronica.state.cooldowns.pop(CREW_ENTITY, None)

    print("\nRun after reset:")
    result = run_crew(crew, veronica, breaker, inputs)
    if result:
        print("  -> Execution resumed successfully after manual reset.")


# ---------------------------------------------------------------------------
# Minimal test suite (runs as __main__)
# ---------------------------------------------------------------------------

def _test_stub_crew_success() -> None:
    crew = _StubCrew(outcomes=[True])
    result = crew.kickoff({})
    assert result is not None, "Expected result on success"
    assert "successfully" in result
    print("[TEST] _test_stub_crew_success: PASS")


def _test_stub_crew_failure() -> None:
    crew = _StubCrew(outcomes=[False])
    try:
        crew.kickoff({})
        assert False, "Expected RuntimeError"
    except RuntimeError:
        pass
    print("[TEST] _test_stub_crew_failure: PASS")


def _test_circuit_breaker_opens() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
    assert breaker.state == CircuitState.CLOSED

    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED

    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    print("[TEST] _test_circuit_breaker_opens: PASS")


def _test_safe_mode_blocks_kickoff() -> None:
    veronica = VeronicaIntegration(
        cooldown_fails=3,
        cooldown_seconds=60,
        auto_save_interval=0,
        backend=MemoryBackend(),
    )
    breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
    crew = _StubCrew(outcomes=[True], name="TestCrew")

    # Activate SAFE_MODE
    veronica.state.transition(VeronicaState.SAFE_MODE, "test")

    result = run_crew(crew, veronica, breaker, {})
    assert result is None, "Expected None when SAFE_MODE active"
    assert crew._call_index == 0, "crew.kickoff() must not have been called"
    print("[TEST] _test_safe_mode_blocks_kickoff: PASS")


def _test_reset_resumes_execution() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
    from veronica_core.runtime_policy import PolicyContext

    # Open the circuit
    breaker.record_failure()
    assert not breaker.check(PolicyContext()).allowed

    # Reset and verify
    breaker.reset()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.check(PolicyContext()).allowed
    print("[TEST] _test_reset_resumes_execution: PASS")


def run_tests() -> None:
    print("\n" + "=" * 60)
    print("Running tests")
    print("=" * 60)
    _test_stub_crew_success()
    _test_stub_crew_failure()
    _test_circuit_breaker_opens()
    _test_safe_mode_blocks_kickoff()
    _test_reset_resumes_execution()
    print("\nAll tests passed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("\nVERONICA Core -- CrewAI Integration Showcase")
    print("VeronicaIntegration + CircuitBreaker wrap crew.kickoff().\n")

    demo_circuit_breaker()
    demo_safe_mode_halt()
    demo_manual_reset()

    print("\n" + "=" * 60)
    print("Key Takeaway:")
    print("  veronica = VeronicaIntegration(cooldown_fails=3, cooldown_seconds=60)")
    print("  breaker  = CircuitBreaker(failure_threshold=3, recovery_timeout=60)")
    print("  # Before each kickoff: check state, circuit, cooldown")
    print("  # After each result:   record_pass/fail + record_success/failure")
    print("=" * 60)

    run_tests()


if __name__ == "__main__":
    main()
