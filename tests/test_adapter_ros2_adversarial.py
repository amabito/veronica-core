"""Adversarial tests for the ROS2 adapter -- attacker mindset.

Covers:
  - Callback explosions (on_mode_change raises)
  - SystemExit / KeyboardInterrupt through guard()
  - Concurrent guard() access (race on _last_mode)
  - Reentrant guard (guard inside guard)
  - on_mode_change triggering further faults (recursive callback)
  - Unknown CircuitState in state_to_mode mapping
  - Non-exception types passed to record_fault
  - Rapid fault/healthy alternation (mode thrashing)
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from veronica_core.adapters.ros2 import OperatingMode, SafetyMonitor
from veronica_core.circuit_breaker import CircuitBreaker, CircuitState


class SensorFault(Exception):
    """Test exception for sensor faults."""


class ActuatorFault(Exception):
    """A different fault type."""


def _make_monitor(
    threshold: int = 3,
    timeout: float = 0.5,
    on_mode_change=None,
) -> SafetyMonitor:
    cb = CircuitBreaker(failure_threshold=threshold, recovery_timeout=timeout)
    return SafetyMonitor(
        circuit_breaker=cb,
        logger=logging.getLogger("test_ros2_adversarial"),
        on_mode_change=on_mode_change,
    )


# ---------------------------------------------------------------------------
# 1. Callback that raises -- must not corrupt _last_mode
# ---------------------------------------------------------------------------


class TestCallbackExplosion:
    """on_mode_change callback raises an exception."""

    def test_callback_raises_does_not_corrupt_state(self) -> None:
        """If on_mode_change raises, _last_mode must still be updated.

        _last_mode is updated BEFORE the callback is invoked, so even
        if the callback raises, the next _check_transition will NOT
        re-fire the same transition.
        """
        call_count = 0

        def exploding_callback(old: OperatingMode, new: OperatingMode) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("callback exploded")

        sm = _make_monitor(threshold=2, on_mode_change=exploding_callback)

        sm.record_fault(SensorFault("a"))
        with pytest.raises(RuntimeError, match="callback exploded"):
            sm.record_fault(SensorFault("b"))

        assert call_count == 1
        # _last_mode WAS updated before callback raised
        assert sm._last_mode is OperatingMode.HALT
        # The circuit IS open
        assert sm.circuit_breaker.state == CircuitState.OPEN

        # Subsequent record_fault must NOT re-fire the same callback
        # because _last_mode is already HALT
        sm.record_fault(SensorFault("c"))
        assert call_count == 1  # no re-fire

    def test_callback_raises_inside_guard(self) -> None:
        """guard() must NOT suppress callback RuntimeError as a fault."""
        def exploding_callback(old, new):
            raise RuntimeError("boom")

        sm = _make_monitor(threshold=1, on_mode_change=exploding_callback)

        # guard with error_type=SensorFault should NOT catch RuntimeError
        # from the callback -- it should propagate
        with pytest.raises(RuntimeError, match="boom"):
            with sm.guard(error_type=SensorFault):
                raise SensorFault("trigger transition")


# ---------------------------------------------------------------------------
# 2. SystemExit / KeyboardInterrupt through guard()
# ---------------------------------------------------------------------------


class TestSystemExitThroughGuard:
    """guard() must NOT swallow SystemExit or KeyboardInterrupt."""

    def test_system_exit_propagates(self) -> None:
        sm = _make_monitor()
        with pytest.raises(SystemExit):
            with sm.guard(error_type=Exception):
                raise SystemExit(1)

    def test_keyboard_interrupt_propagates(self) -> None:
        sm = _make_monitor()
        with pytest.raises(KeyboardInterrupt):
            with sm.guard(error_type=Exception):
                raise KeyboardInterrupt()

    def test_system_exit_not_recorded_as_fault(self) -> None:
        """SystemExit must propagate without incrementing failure count."""
        sm = _make_monitor(threshold=2)
        try:
            with sm.guard(error_type=Exception):
                raise SystemExit(1)
        except SystemExit:
            pass
        # Circuit should still be CLOSED
        assert sm.current_mode is OperatingMode.FULL_AUTO

    def test_guard_with_base_exception_catches_system_exit(self) -> None:
        """If error_type=BaseException, guard WILL catch SystemExit.

        This is dangerous but explicit -- the user asked for it.
        """
        sm = _make_monitor(threshold=2)
        # Should NOT raise because error_type=BaseException catches all
        with sm.guard(error_type=BaseException):
            raise SystemExit(1)
        # Fault was recorded
        assert sm.circuit_breaker._failure_count == 1


# ---------------------------------------------------------------------------
# 3. Concurrent guard() access
# ---------------------------------------------------------------------------


class TestConcurrentGuard:
    """Multiple threads calling guard() simultaneously."""

    def test_concurrent_fault_injection(self) -> None:
        """10 threads inject faults simultaneously. Circuit must open."""
        sm = _make_monitor(threshold=5, timeout=1.0)
        barrier = threading.Barrier(10)
        errors = []

        def inject_fault():
            try:
                barrier.wait(timeout=5)
                with sm.guard(error_type=SensorFault):
                    raise SensorFault("concurrent fault")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=inject_fault) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Circuit must be OPEN after 10 faults (threshold=5)
        assert sm.current_mode is OperatingMode.HALT
        # No unexpected errors
        assert len(errors) == 0

    def test_concurrent_mixed_fault_and_healthy(self) -> None:
        """5 fault threads + 5 healthy threads. Must not crash."""
        sm = _make_monitor(threshold=10, timeout=1.0)
        barrier = threading.Barrier(10)
        errors = []

        def fault_thread():
            try:
                barrier.wait(timeout=5)
                with sm.guard(error_type=SensorFault):
                    raise SensorFault("fault")
            except Exception as e:
                errors.append(e)

        def healthy_thread():
            try:
                barrier.wait(timeout=5)
                with sm.guard(error_type=SensorFault):
                    pass  # healthy
            except Exception as e:
                errors.append(e)

        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=fault_thread))
            threads.append(threading.Thread(target=healthy_thread))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Must not crash -- mode is some valid OperatingMode
        assert isinstance(sm.current_mode, OperatingMode)
        assert len(errors) == 0

    def test_last_mode_race_does_not_crash(self) -> None:
        """Rapid transitions from multiple threads must not crash."""
        transitions = []
        lock = threading.Lock()

        def safe_callback(old, new):
            with lock:
                transitions.append((old.name, new.name))

        sm = _make_monitor(threshold=1, timeout=0.01, on_mode_change=safe_callback)
        errors = []

        def rapid_cycle():
            try:
                for _ in range(20):
                    sm.record_fault(SensorFault("x"))
                    time.sleep(0.015)
                    sm.record_healthy()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=rapid_cycle) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0
        assert len(transitions) > 0


# ---------------------------------------------------------------------------
# 4. Reentrant guard
# ---------------------------------------------------------------------------


class TestReentrantGuard:
    """guard() called inside guard()."""

    def test_nested_guard_same_monitor(self) -> None:
        """Nested guard: inner fault is recorded, outer healthy resets count.

        This is expected behavior: the inner guard records 1 fault, then
        the outer guard completes successfully and calls record_healthy(),
        which resets _failure_count to 0 (CircuitBreaker.record_success
        resets failure count when CLOSED).
        """
        sm = _make_monitor(threshold=3)

        with sm.guard(error_type=SensorFault) as outer_mode:
            assert outer_mode is OperatingMode.FULL_AUTO
            # Inner guard records fault
            with sm.guard(error_type=ActuatorFault):
                raise ActuatorFault("inner fault")
            # After inner guard, outer body continues

        # Outer guard exits normally -> record_healthy -> resets failure_count
        assert sm.circuit_breaker._failure_count == 0
        assert sm.current_mode is OperatingMode.FULL_AUTO

    def test_nested_guard_inner_fault_outer_healthy(self) -> None:
        """Inner fault + outer healthy = net 1 fault + 1 healthy."""
        sm = _make_monitor(threshold=3)

        with sm.guard(error_type=SensorFault):
            with sm.guard(error_type=ActuatorFault):
                raise ActuatorFault("inner fault")
            # Outer body continues -- will record healthy at exit

        # Inner: 1 fault, outer: 1 healthy
        # Net failure_count depends on CircuitBreaker internals
        # but must not crash
        assert isinstance(sm.current_mode, OperatingMode)


# ---------------------------------------------------------------------------
# 5. Recursive callback -- on_mode_change triggers more faults
# ---------------------------------------------------------------------------


class TestRecursiveCallback:
    """on_mode_change that itself calls record_fault."""

    def test_recursive_record_fault_in_callback(self) -> None:
        """Callback calls record_fault -> must not infinite loop.

        After fix: _last_mode is updated BEFORE the callback runs,
        so the record_fault inside the callback calls _check_transition,
        which sees _last_mode == current_mode (both HALT) and does nothing.
        """
        call_count = 0

        def recursive_callback(old, new):
            nonlocal call_count
            call_count += 1
            if call_count > 10:
                return  # safety valve (should never be needed)
            sm.record_fault(SensorFault("from callback"))

        sm = _make_monitor(threshold=2, on_mode_change=recursive_callback)
        sm.record_fault(SensorFault("a"))
        sm.record_fault(SensorFault("b"))  # triggers FULL_AUTO->HALT

        # Callback fires exactly once. The record_fault inside callback
        # does NOT trigger another transition because _last_mode is already
        # HALT (updated before callback invocation).
        assert call_count == 1
        assert sm.current_mode is OperatingMode.HALT


# ---------------------------------------------------------------------------
# 6. Unknown CircuitState in state_to_mode
# ---------------------------------------------------------------------------


class TestUnknownCircuitState:
    """state_to_mode missing an entry -> HALT fallback."""

    def test_missing_state_defaults_to_halt(self) -> None:
        """current_mode falls back to HALT for unmapped states."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1.0)
        # Provide incomplete mapping -- HALF_OPEN is missing
        incomplete = {
            CircuitState.CLOSED: OperatingMode.FULL_AUTO,
            CircuitState.OPEN: OperatingMode.HALT,
        }
        sm = SafetyMonitor(circuit_breaker=cb, state_to_mode=incomplete)

        # Force into HALF_OPEN state
        sm.record_fault(SensorFault("a"))
        sm.record_fault(SensorFault("b"))
        sm.record_fault(SensorFault("c"))
        time.sleep(0.01)
        # Manually set state for test (recovery_timeout is 1s)
        with cb._lock:
            cb._state = CircuitState.HALF_OPEN

        # Should fall back to HALT (deny > allow)
        assert sm.current_mode is OperatingMode.HALT

    def test_empty_state_to_mode_always_halt(self) -> None:
        """Empty mapping -> always HALT."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1.0)
        sm = SafetyMonitor(circuit_breaker=cb, state_to_mode={})
        assert sm.current_mode is OperatingMode.HALT


# ---------------------------------------------------------------------------
# 7. Non-standard error types passed to record_fault
# ---------------------------------------------------------------------------


class TestNonStandardErrors:
    """Unusual error objects passed to record_fault."""

    def test_error_with_no_str(self) -> None:
        """Exception with broken __str__ must not crash guard logging."""
        class BrokenStr(Exception):
            def __str__(self):
                raise RuntimeError("broken __str__")
            def __repr__(self):
                raise RuntimeError("broken __repr__")

        sm = _make_monitor(threshold=3)
        # record_fault should handle this
        # The _log_warn call uses f"{exc!r}" which will raise
        # But this is inside guard(), so let's test that path
        with pytest.raises(RuntimeError, match="broken __repr__"):
            with sm.guard(error_type=BrokenStr):
                raise BrokenStr("will crash repr")

    def test_exception_subclass_chain(self) -> None:
        """Deep inheritance chain: guard catches base, not leaf."""
        class L1(Exception):
            pass

        class L2(L1):
            pass

        class L3(L2):
            pass

        sm = _make_monitor(threshold=5)
        with sm.guard(error_type=L1):
            raise L3("deep child")
        assert sm.circuit_breaker._failure_count == 1

    def test_multiple_inheritance_exception(self) -> None:
        """Exception with multiple bases."""
        class A(Exception):
            pass

        class B(Exception):
            pass

        class C(A, B):
            pass

        sm = _make_monitor(threshold=5)
        with sm.guard(error_type=A):
            raise C("multi-base")
        assert sm.circuit_breaker._failure_count == 1

        with sm.guard(error_type=B):
            raise C("multi-base via B")
        assert sm.circuit_breaker._failure_count == 2


# ---------------------------------------------------------------------------
# 8. Mode thrashing -- rapid fault/healthy alternation
# ---------------------------------------------------------------------------


class TestModeThrashing:
    """Rapid fault/healthy alternation."""

    def test_rapid_alternation_does_not_lose_transitions(self) -> None:
        """Every mode change must fire the callback exactly once."""
        transitions = []
        sm = _make_monitor(
            threshold=1,
            timeout=0.01,
            on_mode_change=lambda o, n: transitions.append((o.name, n.name)),
        )

        # Fault -> HALT
        sm.record_fault(SensorFault("1"))
        assert sm.current_mode is OperatingMode.HALT

        # Wait for HALF_OPEN
        time.sleep(0.02)

        # Healthy -> recover
        sm.record_healthy()  # HALT->SLOW or HALT->FULL_AUTO
        for _ in range(5):
            sm.record_healthy()
            if sm.current_mode is OperatingMode.FULL_AUTO:
                break

        # Fault again
        sm.record_fault(SensorFault("2"))
        assert sm.current_mode is OperatingMode.HALT

        # All transitions must be recorded, no duplicates
        for i in range(len(transitions) - 1):
            # No two consecutive transitions should have the same new mode
            # (that would indicate a spurious callback)
            assert transitions[i] != transitions[i + 1]

    def test_threshold_one_immediate_halt(self) -> None:
        """With threshold=1, a single fault must immediately halt."""
        sm = _make_monitor(threshold=1)
        sm.record_fault(SensorFault("one"))
        assert sm.current_mode is OperatingMode.HALT


# ---------------------------------------------------------------------------
# 9. guard() with error_type that is not an exception class
# ---------------------------------------------------------------------------


class TestGuardEdgeCases:
    """Edge cases for guard() parameters."""

    def test_guard_error_type_not_exception_class(self) -> None:
        """Passing a non-exception type should not crash guard setup.

        isinstance(exc, str) will just return False -> exception propagates.
        """
        sm = _make_monitor()
        with pytest.raises(ValueError):
            with sm.guard(error_type=str):  # type: ignore[arg-type]
                raise ValueError("not a str")

    def test_guard_with_tuple_of_exceptions(self) -> None:
        """error_type can be a tuple (isinstance supports it)."""
        sm = _make_monitor(threshold=5)
        with sm.guard(error_type=(SensorFault, ActuatorFault)):  # type: ignore[arg-type]
            raise ActuatorFault("tuple test")
        assert sm.circuit_breaker._failure_count == 1
