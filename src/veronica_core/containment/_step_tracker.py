"""Internal step counter for ExecutionContext.

_StepTracker owns the step count integer and its lock.
This module is package-internal (_-prefix); do NOT import it from outside
veronica_core.containment.
"""

from __future__ import annotations

import threading


class StepTracker:
    """Thread-safe step counter.

    Owns a single integer counter protected by an internal lock.
    All read-modify-write operations are atomic relative to the lock.
    """

    def __init__(self) -> None:
        self._count: int = 0
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        """Return the current step count."""
        with self._lock:
            return self._count

    def increment(self) -> None:
        """Increment step counter by 1."""
        with self._lock:
            self._count += 1

    def increment_returning(self) -> int:
        """Increment step counter by 1 and return the new value atomically."""
        with self._lock:
            self._count += 1
            return self._count

    def set(self, value: int) -> None:
        """Set step count to an absolute *value* (for test setup and compatibility)."""
        if isinstance(value, bool):
            raise TypeError(
                f"StepTracker.set() value must be an int, not bool, got {value!r}"
            )
        if not isinstance(value, int):
            raise TypeError(
                f"StepTracker.set() value must be an int, got {type(value).__name__!r}"
            )
        if value < 0:
            raise ValueError(
                f"StepTracker.set() value must be non-negative, got {value!r}"
            )
        with self._lock:
            self._count = value

    def check(self, max_steps: int) -> str | None:
        """Return "step_limit_exceeded" if count >= max_steps, else None.

        Args:
            max_steps: The maximum number of steps allowed.

        Returns:
            "step_limit_exceeded" if the limit is reached; None otherwise.
        """
        with self._lock:
            count = self._count
        if count >= max_steps:
            return "step_limit_exceeded"
        return None
