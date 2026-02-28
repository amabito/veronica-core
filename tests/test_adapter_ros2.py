"""Tests for the ROS2 adapter (SafetyMonitor + OperatingMode).

These tests run without rclpy -- only veronica_core internals are tested.
"""

from __future__ import annotations

import logging
import time

from veronica_core.adapters.ros2 import OperatingMode, SafetyMonitor, _STATE_TO_MODE
from veronica_core.circuit_breaker import CircuitBreaker, CircuitState


# ---------------------------------------------------------------------------
# OperatingMode enum
# ---------------------------------------------------------------------------


class TestOperatingMode:
    """OperatingMode enum behaviour."""

    def test_speed_scales(self) -> None:
        assert OperatingMode.FULL_AUTO.speed_scale == 1.0
        assert OperatingMode.CAUTIOUS.speed_scale == 0.5
        assert OperatingMode.SLOW.speed_scale == 0.15
        assert OperatingMode.HALT.speed_scale == 0.0

    def test_descriptions(self) -> None:
        for mode in OperatingMode:
            assert isinstance(mode.description, str)
            assert len(mode.description) > 0

    def test_ordering_degrade(self) -> None:
        scales = [m.speed_scale for m in OperatingMode]
        assert scales == sorted(scales, reverse=True)

    def test_member_count(self) -> None:
        assert len(OperatingMode) == 4


# ---------------------------------------------------------------------------
# State-to-mode mapping
# ---------------------------------------------------------------------------


class TestStateToMode:
    """Default CircuitState -> OperatingMode mapping."""

    def test_closed_maps_to_full_auto(self) -> None:
        assert _STATE_TO_MODE[CircuitState.CLOSED] is OperatingMode.FULL_AUTO

    def test_half_open_maps_to_slow(self) -> None:
        assert _STATE_TO_MODE[CircuitState.HALF_OPEN] is OperatingMode.SLOW

    def test_open_maps_to_halt(self) -> None:
        assert _STATE_TO_MODE[CircuitState.OPEN] is OperatingMode.HALT


# ---------------------------------------------------------------------------
# SafetyMonitor -- basic API
# ---------------------------------------------------------------------------


class SensorFault(Exception):
    """Test exception for sensor faults."""


def _make_monitor(
    threshold: int = 3,
    timeout: float = 0.5,
    on_mode_change=None,
) -> SafetyMonitor:
    cb = CircuitBreaker(failure_threshold=threshold, recovery_timeout=timeout)
    return SafetyMonitor(
        circuit_breaker=cb,
        logger=logging.getLogger("test_ros2"),
        on_mode_change=on_mode_change,
    )


class TestSafetyMonitorBasic:
    """SafetyMonitor core API."""

    def test_initial_mode_is_full_auto(self) -> None:
        sm = _make_monitor()
        assert sm.current_mode is OperatingMode.FULL_AUTO

    def test_record_fault_returns_true(self) -> None:
        sm = _make_monitor()
        assert sm.record_fault(SensorFault("boom")) is True

    def test_record_healthy_does_not_raise(self) -> None:
        sm = _make_monitor()
        sm.record_healthy()  # should not raise

    def test_faults_open_circuit(self) -> None:
        sm = _make_monitor(threshold=3)
        for _ in range(3):
            sm.record_fault(SensorFault("x"))
        assert sm.current_mode is OperatingMode.HALT

    def test_recovery_after_timeout(self) -> None:
        sm = _make_monitor(threshold=2, timeout=0.1)
        sm.record_fault(SensorFault("a"))
        sm.record_fault(SensorFault("b"))
        assert sm.current_mode is OperatingMode.HALT

        time.sleep(0.15)
        # After timeout, circuit goes HALF_OPEN -> SLOW
        assert sm.current_mode is OperatingMode.SLOW

    def test_custom_state_to_mode(self) -> None:
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1.0)
        custom = {
            CircuitState.CLOSED: OperatingMode.CAUTIOUS,
            CircuitState.HALF_OPEN: OperatingMode.HALT,
            CircuitState.OPEN: OperatingMode.HALT,
        }
        sm = SafetyMonitor(circuit_breaker=cb, state_to_mode=custom)
        assert sm.current_mode is OperatingMode.CAUTIOUS


# ---------------------------------------------------------------------------
# SafetyMonitor -- guard context manager
# ---------------------------------------------------------------------------


class TestSafetyMonitorGuard:
    """SafetyMonitor.guard() context manager."""

    def test_guard_yields_current_mode(self) -> None:
        sm = _make_monitor()
        with sm.guard() as mode:
            assert mode is OperatingMode.FULL_AUTO

    def test_guard_suppresses_matching_exception(self) -> None:
        sm = _make_monitor()
        with sm.guard(error_type=SensorFault) as _mode:
            raise SensorFault("corrupted")
        # No exception propagated

    def test_guard_propagates_non_matching_exception(self) -> None:
        sm = _make_monitor()
        try:
            with sm.guard(error_type=SensorFault):
                raise ValueError("unrelated")
            assert False, "Should have propagated"
        except ValueError:
            pass

    def test_guard_records_fault_on_suppression(self) -> None:
        sm = _make_monitor(threshold=2)
        with sm.guard(error_type=SensorFault):
            raise SensorFault("a")
        with sm.guard(error_type=SensorFault):
            raise SensorFault("b")
        assert sm.current_mode is OperatingMode.HALT

    def test_guard_records_healthy_on_success(self) -> None:
        sm = _make_monitor(threshold=2, timeout=0.1)
        sm.record_fault(SensorFault("a"))
        sm.record_fault(SensorFault("b"))
        time.sleep(0.15)

        # HALF_OPEN -> guard with no exception -> record_healthy -> CLOSED
        with sm.guard(error_type=SensorFault):
            pass  # healthy
        assert sm.current_mode is OperatingMode.FULL_AUTO

    def test_guard_no_healthy_in_halt_mode(self) -> None:
        sm = _make_monitor(threshold=2)
        sm.record_fault(SensorFault("a"))
        sm.record_fault(SensorFault("b"))
        assert sm.current_mode is OperatingMode.HALT

        # guard in HALT mode should NOT record healthy
        with sm.guard(error_type=SensorFault):
            pass
        assert sm.current_mode is OperatingMode.HALT


# ---------------------------------------------------------------------------
# SafetyMonitor -- mode change callback
# ---------------------------------------------------------------------------


class TestModeChangeCallback:
    """on_mode_change callback."""

    def test_callback_called_on_transition(self) -> None:
        transitions: list[tuple[OperatingMode, OperatingMode]] = []

        def on_change(old: OperatingMode, new: OperatingMode) -> None:
            transitions.append((old, new))

        sm = _make_monitor(threshold=2, on_mode_change=on_change)
        sm.record_fault(SensorFault("a"))
        sm.record_fault(SensorFault("b"))

        assert len(transitions) == 1
        assert transitions[0] == (OperatingMode.FULL_AUTO, OperatingMode.HALT)

    def test_callback_not_called_without_transition(self) -> None:
        transitions: list = []
        sm = _make_monitor(
            threshold=5,
            on_mode_change=lambda o, n: transitions.append((o, n)),
        )
        sm.record_fault(SensorFault("a"))  # 1 fault, still CLOSED
        assert len(transitions) == 0

    def test_full_lifecycle_transitions(self) -> None:
        transitions: list[tuple[OperatingMode, OperatingMode]] = []

        sm = _make_monitor(
            threshold=2,
            timeout=0.1,
            on_mode_change=lambda o, n: transitions.append((o, n)),
        )

        # Degrade: FULL_AUTO -> HALT
        sm.record_fault(SensorFault("a"))
        sm.record_fault(SensorFault("b"))
        assert sm.current_mode is OperatingMode.HALT

        # Wait for timeout -> HALF_OPEN (SLOW)
        time.sleep(0.15)
        # First record_healthy triggers HALT -> SLOW transition
        sm.record_healthy()
        assert sm.current_mode is OperatingMode.SLOW

        # HALF_OPEN requires multiple successes to close;
        # keep recording healthy until CLOSED
        for _ in range(10):
            sm.record_healthy()
            if sm.current_mode is OperatingMode.FULL_AUTO:
                break

        assert sm.current_mode is OperatingMode.FULL_AUTO

        # Transitions: FULL_AUTO->HALT, HALT->SLOW, SLOW->FULL_AUTO
        assert len(transitions) == 3
        assert transitions[0] == (OperatingMode.FULL_AUTO, OperatingMode.HALT)
        assert transitions[1] == (OperatingMode.HALT, OperatingMode.SLOW)
        assert transitions[2] == (OperatingMode.SLOW, OperatingMode.FULL_AUTO)


# ---------------------------------------------------------------------------
# SafetyMonitor -- logger fallback
# ---------------------------------------------------------------------------


class TestLoggerFallback:
    """Logger defaults to module-level logger when not provided."""

    def test_default_logger(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1.0)
        sm = SafetyMonitor(circuit_breaker=cb)
        # Should not raise when logging
        sm.record_fault(SensorFault("test"))

    def test_logger_with_warn_method(self) -> None:
        class FakeLogger:
            def __init__(self):
                self.messages = []
            def info(self, msg):
                self.messages.append(("info", msg))
            def warn(self, msg):
                self.messages.append(("warn", msg))

        fake = FakeLogger()
        sm = SafetyMonitor(
            circuit_breaker=CircuitBreaker(failure_threshold=2, recovery_timeout=1.0),
            logger=fake,
        )
        with sm.guard(error_type=SensorFault):
            raise SensorFault("test")

        assert any("Fault suppressed" in m[1] for m in fake.messages)

