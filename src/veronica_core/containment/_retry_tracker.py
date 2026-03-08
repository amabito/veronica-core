"""Internal retry counter for ExecutionContext.

_RetryTracker owns the retry count integer and its lock.
This module is package-internal (_-prefix); do NOT import it from outside
veronica_core.containment.
"""

from __future__ import annotations

import threading


class RetryTracker:
    """Thread-safe retry counter.

    Owns a single integer counter protected by an internal lock.
    """

    def __init__(self) -> None:
        self._count: int = 0
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        """Return the current retry count."""
        with self._lock:
            return self._count

    def increment(self) -> None:
        """Increment retry counter by 1."""
        with self._lock:
            self._count += 1

    def check(self, max_retries: int) -> str | None:
        """Return "retry_budget_exceeded" if count >= max_retries, else None.

        Args:
            max_retries: The maximum number of retries allowed.

        Returns:
            "retry_budget_exceeded" if the budget is exhausted; None otherwise.
        """
        with self._lock:
            count = self._count
        if count >= max_retries:
            return "retry_budget_exceeded"
        return None
