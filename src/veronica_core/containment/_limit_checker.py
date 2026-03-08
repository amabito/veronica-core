"""Internal limit-checking helper for ExecutionContext.

_LimitChecker owns the mutable chain-level counters (step_count,
cost_usd_accumulated, retries_used, aborted flag, abort_reason, start_time)
and exposes thread-safe accessors plus a single check_limits() entry-point.

This module is package-internal (_-prefix); do NOT import it from outside
veronica_core.containment.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from veronica_core.containment.types import CancellationToken, ExecutionConfig

logger = logging.getLogger(__name__)


class _LimitChecker:
    """Thread-safe container for chain-level counters and limit enforcement.

    Holds:
        step_count, cost_usd_accumulated, retries_used, aborted flag,
        abort_reason, start_time.

    The caller (ExecutionContext) retains its own outer _lock for operations
    that span multiple helpers; _LimitChecker has its own internal lock for
    its own state.
    """

    def __init__(
        self,
        config: "ExecutionConfig",
        cancellation_token: "CancellationToken",
    ) -> None:
        self._config = config
        self._cancellation_token = cancellation_token
        self._lock = threading.Lock()

        self._step_count: int = 0
        self._cost_usd_accumulated: float = 0.0
        self._retries_used: int = 0
        self._aborted: bool = False
        self._abort_reason: str | None = None
        self._start_time: float = time.monotonic()

    # ------------------------------------------------------------------
    # Read-only properties (each acquires the internal lock)
    # ------------------------------------------------------------------

    @property
    def step_count(self) -> int:
        with self._lock:
            return self._step_count

    @property
    def cost_usd_accumulated(self) -> float:
        with self._lock:
            return self._cost_usd_accumulated

    @property
    def retries_used(self) -> int:
        with self._lock:
            return self._retries_used

    @property
    def is_aborted(self) -> bool:
        with self._lock:
            return self._aborted

    @property
    def abort_reason(self) -> str | None:
        with self._lock:
            return self._abort_reason

    @property
    def elapsed_ms(self) -> float:
        # _start_time is write-once, but lock for consistency with other properties.
        with self._lock:
            return (time.monotonic() - self._start_time) * 1000.0

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def set_cost(self, value: float) -> None:
        """Set cost to an absolute value (for test setup and compatibility)."""
        with self._lock:
            self._cost_usd_accumulated = value

    def set_step_count(self, value: int) -> None:
        """Set step count to an absolute value (for test setup and compatibility)."""
        with self._lock:
            self._step_count = value

    def increment_step(self) -> None:
        """Increment step counter by 1 (called on successful wrap completion)."""
        with self._lock:
            self._step_count += 1

    def add_cost(self, amount: float) -> None:
        """Add *amount* to the accumulated cost (called on successful wrap completion)."""
        with self._lock:
            self._cost_usd_accumulated += amount

    def commit_success(self, cost: float) -> None:
        """Atomically increment step count and add cost (single lock acquisition)."""
        with self._lock:
            self._step_count += 1
            self._cost_usd_accumulated += cost

    def increment_step_returning(self) -> int:
        """Increment step counter by 1 and return the new value atomically."""
        with self._lock:
            self._step_count += 1
            return self._step_count

    def add_cost_returning(self, amount: float) -> float:
        """Add *amount* and return the new accumulated cost atomically."""
        with self._lock:
            self._cost_usd_accumulated += amount
            return self._cost_usd_accumulated

    def increment_retries(self) -> None:
        """Increment retry counter by 1 (called when RETRY decision is taken)."""
        with self._lock:
            self._retries_used += 1

    def mark_aborted(self, reason: str) -> bool:
        """Set aborted flag and reason.

        Returns True if this call actually changed the state (first abort),
        False if already aborted.  Thread-safe and idempotent.
        """
        with self._lock:
            if self._aborted:
                return False
            self._aborted = True
            self._abort_reason = reason
            return True

    def mark_closed(self) -> bool:
        """Mark as aborted with reason 'context_closed'.

        Returns True if this was the first close (same semantics as mark_aborted).
        """
        return self.mark_aborted("context_closed")

    def add_cost_and_get_total(self, amount: float) -> float:
        """Add *amount* and return the new total atomically (single lock).

        Used by _propagate_child_cost to avoid TOCTOU between add and read.
        """
        with self._lock:
            self._cost_usd_accumulated += amount
            return self._cost_usd_accumulated

    # ------------------------------------------------------------------
    # Snapshot helpers (no lock -- caller holds outer lock when needed)
    # ------------------------------------------------------------------

    def snapshot_counters(self) -> dict[str, Any]:
        """Return a dict of all counter values under the internal lock."""
        with self._lock:
            return {
                "step_count": self._step_count,
                "cost_usd_accumulated": self._cost_usd_accumulated,
                "retries_used": self._retries_used,
                "aborted": self._aborted,
                "abort_reason": self._abort_reason,
                "elapsed_ms": (time.monotonic() - self._start_time) * 1000.0,
            }

    # ------------------------------------------------------------------
    # Limit checking
    # ------------------------------------------------------------------

    def check_limits(
        self,
        budget_backend: Any,
        emit_fn: Any,
    ) -> str | None:
        """Return a stop-reason string if any chain-level limit is exceeded.

        Returns None when all limits are within bounds.

        Checked in priority order:
            1. aborted flag
            2. cost ceiling (local)
            3. step limit
            4. retry budget
            5. cross-process budget (if distributed backend)
            6. timeout / cancellation

        Args:
            budget_backend: The budget backend (LocalBudgetBackend or distributed).
            emit_fn: Callable(stop_reason: str, detail: str) that emits a
                chain event.  Always called outside the internal lock;
                counters are snapshotted under the lock first.
        """
        from veronica_core.distributed import LocalBudgetBackend, _BUDGET_EPSILON

        # Snapshot counters under the lock, then emit events outside the lock
        # to avoid holding the lock during potentially slow I/O (emit_fn may
        # acquire other locks).
        with self._lock:
            if self._aborted:
                return "aborted"

            cost = self._cost_usd_accumulated
            steps = self._step_count
            retries = self._retries_used
            max_cost = self._config.max_cost_usd
            max_steps = self._config.max_steps
            max_retries = self._config.max_retries_total

        if cost + _BUDGET_EPSILON >= max_cost:
            reason = f"cost ${cost:.4f} >= ceiling ${max_cost:.4f}"
            emit_fn("budget_exceeded", reason)
            return "budget_exceeded"

        if steps >= max_steps:
            reason = f"steps {steps} >= limit {max_steps}"
            emit_fn("step_limit_exceeded", reason)
            return "step_limit_exceeded"

        if retries >= max_retries:
            reason = f"retries {retries} >= budget {max_retries}"
            emit_fn("retry_budget_exceeded", reason)
            return "retry_budget_exceeded"

        # Cross-process budget check -- outside lock to avoid blocking other
        # threads during a potentially slow Redis round-trip (H4).
        if not isinstance(budget_backend, LocalBudgetBackend):
            try:
                global_total = budget_backend.get()
                if global_total + _BUDGET_EPSILON >= self._config.max_cost_usd:
                    reason = (
                        f"cross-process cost ${global_total:.4f} >= "
                        f"ceiling ${self._config.max_cost_usd:.4f}"
                    )
                    emit_fn("budget_exceeded", reason)
                    return "budget_exceeded"
            except Exception:
                logger.debug(
                    "_LimitChecker: budget backend unavailable during cross-process check; "
                    "falling back to local limit",
                    exc_info=True,
                )

        if self._cancellation_token.is_cancelled:
            emit_fn("timeout", "cancellation token signalled")
            return "timeout"

        return None
