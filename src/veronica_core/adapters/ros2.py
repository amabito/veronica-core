"""VERONICA adapter for ROS2 (rclpy).

Provides a thin safety wrapper around rclpy node callbacks using
CircuitBreaker for fault detection and a simple degradation enum
for mode transitions.

Requires: rclpy (not a veronica-core dependency; install separately
via ``sudo apt install ros-jazzy-rclpy`` or equivalent).

Usage::

    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import LaserScan

    from veronica_core import CircuitBreaker
    from veronica_core.adapters.ros2 import SafetyMonitor, OperatingMode

    class MyNode(Node):
        def __init__(self):
            super().__init__('my_node')
            self.safety = SafetyMonitor(
                circuit_breaker=CircuitBreaker(failure_threshold=5, recovery_timeout=10.0),
                logger=self.get_logger(),
            )
            self.create_subscription(LaserScan, '/scan', self.on_scan, 10)

        def on_scan(self, msg):
            with self.safety.guard(error_type=SensorFault) as mode:
                if mode == OperatingMode.HALT:
                    return  # skip processing entirely
                process(msg, speed_scale=mode.speed_scale)
"""

from __future__ import annotations

import enum
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generator, Optional

from veronica_core.circuit_breaker import CircuitBreaker, CircuitState

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class OperatingMode(enum.Enum):
    """Degradation levels for a robotic node.

    Each mode carries a ``speed_scale`` hint that callbacks can use
    to throttle actuator commands.

    Transition order (degrade): FULL_AUTO -> CAUTIOUS -> SLOW -> HALT
    Transition order (recover): HALT -> SLOW -> CAUTIOUS -> FULL_AUTO
    """

    FULL_AUTO = (1.0, "full autonomous operation")
    CAUTIOUS = (0.5, "reduced speed, heightened monitoring")
    SLOW = (0.15, "crawl speed, minimal actuation")
    HALT = (0.0, "emergency stop, no actuation")

    def __init__(self, speed_scale: float, description: str) -> None:
        self.speed_scale = speed_scale
        self.description = description


# Map CircuitBreaker state to a default operating mode.
_STATE_TO_MODE: dict[CircuitState, OperatingMode] = {
    CircuitState.CLOSED: OperatingMode.FULL_AUTO,
    CircuitState.HALF_OPEN: OperatingMode.SLOW,
    CircuitState.OPEN: OperatingMode.HALT,
}


@dataclass
class SafetyMonitor:
    """Lightweight safety wrapper around a :class:`CircuitBreaker`.

    Designed for ROS2 callback-based architectures.  The monitor
    translates CircuitBreaker state into an :class:`OperatingMode`
    that node callbacks can query to decide how (or whether) to act.

    Parameters
    ----------
    circuit_breaker:
        The underlying ``CircuitBreaker`` instance.
    state_to_mode:
        Optional override mapping from ``CircuitState`` to
        ``OperatingMode``.  Defaults to a sensible mapping
        (CLOSED->FULL_AUTO, HALF_OPEN->SLOW, OPEN->HALT).
    logger:
        Optional ROS2 logger (``node.get_logger()``).  Falls back to
        the standard ``logging`` module when not provided.
    on_mode_change:
        Optional callback invoked with ``(old_mode, new_mode)`` on
        every mode transition.
    """

    circuit_breaker: CircuitBreaker
    state_to_mode: dict[CircuitState, OperatingMode] = field(
        default_factory=lambda: dict(_STATE_TO_MODE),
    )
    logger: object = None  # rclpy logger or stdlib logger
    on_mode_change: Optional[object] = None  # Callable[[OperatingMode, OperatingMode], None]

    def __post_init__(self) -> None:
        self._last_mode: OperatingMode = self.current_mode
        if self.logger is None:
            self.logger = logger

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def current_mode(self) -> OperatingMode:
        """Derive the operating mode from the circuit breaker state."""
        return self.state_to_mode.get(
            self.circuit_breaker.state, OperatingMode.HALT
        )

    def record_fault(self, error: BaseException) -> bool:
        """Record a sensor/actuator fault.

        Returns True if the fault was counted (not filtered by a
        ``failure_predicate`` on the circuit breaker).
        """
        counted = self.circuit_breaker.record_failure(error=error)
        self._check_transition()
        return counted

    def record_healthy(self) -> None:
        """Record a healthy reading, potentially recovering the circuit."""
        self.circuit_breaker.record_success()
        self._check_transition()

    @contextmanager
    def guard(
        self,
        error_type: type[BaseException] = Exception,
    ) -> Generator[OperatingMode, None, None]:
        """Context manager for wrapping a callback body.

        Yields the current :class:`OperatingMode`.  If the body raises
        an exception matching *error_type*, it is recorded as a fault
        and suppressed.  Other exceptions propagate normally.

        Example::

            with self.safety.guard(error_type=SensorFault) as mode:
                if mode == OperatingMode.HALT:
                    return
                process(msg, speed_scale=mode.speed_scale)
        """
        mode = self.current_mode
        try:
            yield mode
        except BaseException as exc:
            if isinstance(exc, error_type):
                self.record_fault(exc)
                self._log_warn(f"Fault suppressed: {exc!r}")
            else:
                raise
        else:
            # No exception -- sensor/actuator is healthy.
            if mode != OperatingMode.HALT:
                self.record_healthy()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_transition(self) -> None:
        new_mode = self.current_mode
        if new_mode != self._last_mode:
            self._log_info(
                f"Mode transition: {self._last_mode.name} -> {new_mode.name} "
                f"(speed_scale={new_mode.speed_scale})"
            )
            if self.on_mode_change is not None:
                self.on_mode_change(self._last_mode, new_mode)
            self._last_mode = new_mode

    def _log_info(self, msg: str) -> None:
        _logger = self.logger
        if hasattr(_logger, "info"):
            _logger.info(f"[VERONICA] {msg}")

    def _log_warn(self, msg: str) -> None:
        _logger = self.logger
        if hasattr(_logger, "warning"):
            _logger.warning(f"[VERONICA] {msg}")
        elif hasattr(_logger, "warn"):
            _logger.warn(f"[VERONICA] {msg}")
