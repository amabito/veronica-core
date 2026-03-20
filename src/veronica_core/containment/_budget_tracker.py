"""Internal USD cost accumulator for ExecutionContext.

_BudgetTracker owns the cost float and its lock.  All mutations are atomic.
This module is package-internal (_-prefix); do NOT import it from outside
veronica_core.containment.
"""

from __future__ import annotations

import math
import threading


class BudgetTracker:
    """Thread-safe USD cost accumulator.

    Owns a single float counter protected by an internal lock.
    All read-modify-write operations are atomic relative to the lock.
    """

    def __init__(self) -> None:
        self._cost: float = 0.0
        self._lock = threading.Lock()

    @property
    def cost(self) -> float:
        """Return the current accumulated cost."""
        with self._lock:
            return self._cost

    @staticmethod
    def _validate_amount(amount: float, method: str = "add") -> None:
        """Reject non-finite or negative amounts before mutation."""
        if not math.isfinite(amount):
            raise ValueError(
                f"BudgetTracker.{method}() amount must be a finite number, got {amount!r}"
            )
        if amount < 0:
            raise ValueError(
                f"BudgetTracker.{method}() amount must be non-negative, got {amount!r}"
            )

    def add(self, amount: float) -> None:
        """Add *amount* to the accumulated cost."""
        self._validate_amount(amount, "add")
        with self._lock:
            self._cost += amount

    def add_returning(self, amount: float) -> float:
        """Add *amount* and return the new total atomically."""
        self._validate_amount(amount, "add_returning")
        with self._lock:
            self._cost += amount
            return self._cost

    def set(self, value: float) -> None:
        """Set cost to an absolute *value* (for test setup and compatibility)."""
        with self._lock:
            self._cost = value

    def check(self, max_cost: float, epsilon: float) -> str | None:
        """Return "budget_exceeded" if cost + epsilon >= max_cost, else None.

        Args:
            max_cost: The ceiling in USD.
            epsilon: Small tolerance added to the current cost before comparing.

        Returns:
            "budget_exceeded" if the ceiling is reached; None otherwise.
        """
        with self._lock:
            cost = self._cost
        if cost + epsilon >= max_cost:
            return "budget_exceeded"
        return None
