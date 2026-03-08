"""Internal limit-checking helper for ExecutionContext.

_LimitChecker composes four focused tracker classes:
    BudgetTracker  -- USD cost accumulation
    StepTracker    -- step counter
    RetryTracker   -- retry counter
    TimeoutManager -- cancellation token + timeout watcher

The abort flag and abort_reason are owned here (not in a tracker) because they
span the combined state of all trackers.

This module is package-internal (_-prefix); do NOT import it from outside
veronica_core.containment.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from veronica_core.containment._budget_tracker import BudgetTracker
from veronica_core.containment._retry_tracker import RetryTracker
from veronica_core.containment._step_tracker import StepTracker
from veronica_core.containment._timeout_manager import TimeoutManager

if TYPE_CHECKING:
    from veronica_core.containment.types import CancellationToken, ExecutionConfig

logger = logging.getLogger(__name__)


class _LimitChecker:
    """Thread-safe container for chain-level counters and limit enforcement.

    Composes:
        self.budget   -- BudgetTracker (USD cost)
        self.steps    -- StepTracker (step counter)
        self.retries  -- RetryTracker (retry counter)
        self.timeout  -- TimeoutManager (cancellation + elapsed time)

    The caller (ExecutionContext) retains its own outer _lock for operations
    that span multiple helpers; _LimitChecker has its own internal lock for
    the abort flag only.
    """

    def __init__(
        self,
        config: "ExecutionConfig",
        cancellation_token: "CancellationToken",
    ) -> None:
        self._config = config
        self._lock = threading.Lock()  # Guards _aborted and _abort_reason only.
        self._aborted: bool = False
        self._abort_reason: str | None = None

        # Composed trackers -- each owns its own internal lock.
        self.budget = BudgetTracker()
        self.steps = StepTracker()
        self.retries = RetryTracker()
        self.timeout = TimeoutManager(cancellation_token)

    # ------------------------------------------------------------------
    # Delegated read-only properties
    # ------------------------------------------------------------------

    @property
    def step_count(self) -> int:
        return self.steps.count

    @property
    def cost_usd_accumulated(self) -> float:
        return self.budget.cost

    @property
    def retries_used(self) -> int:
        return self.retries.count

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
        return self.timeout.elapsed_ms

    # ------------------------------------------------------------------
    # Delegated mutators
    # ------------------------------------------------------------------

    def set_cost(self, value: float) -> None:
        """Set cost to an absolute value (for test setup and compatibility)."""
        self.budget.set(value)

    def set_step_count(self, value: int) -> None:
        """Set step count to an absolute value (for test setup and compatibility)."""
        self.steps.set(value)

    def increment_step(self) -> None:
        """Increment step counter by 1 (called on successful wrap completion)."""
        self.steps.increment()

    def add_cost(self, amount: float) -> None:
        """Add *amount* to the accumulated cost (called on successful wrap completion)."""
        self.budget.add(amount)

    def commit_success(self, cost: float) -> None:
        """Atomically increment step count and add cost.

        Each operation acquires its own tracker lock separately; they are not
        combined into a single atomic unit across both trackers.  This is safe
        because no caller relies on observing cost and steps changing together
        in a single lock acquisition.
        """
        self.steps.increment()
        self.budget.add(cost)

    def increment_step_returning(self) -> int:
        """Increment step counter by 1 and return the new value atomically."""
        return self.steps.increment_returning()

    def add_cost_returning(self, amount: float) -> float:
        """Add *amount* and return the new accumulated cost atomically."""
        return self.budget.add_returning(amount)

    def increment_retries(self) -> None:
        """Increment retry counter by 1 (called when RETRY decision is taken)."""
        self.retries.increment()

    # ------------------------------------------------------------------
    # Abort flag
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot_counters(self) -> dict[str, Any]:
        """Return a dict of all counter values.

        Each tracker is read under its own lock; the snapshot is not
        globally atomic across all four trackers, but is consistent
        enough for diagnostic and serialisation purposes.
        """
        with self._lock:
            aborted = self._aborted
            abort_reason = self._abort_reason
        return {
            "step_count": self.steps.count,
            "cost_usd_accumulated": self.budget.cost,
            "retries_used": self.retries.count,
            "aborted": aborted,
            "abort_reason": abort_reason,
            "elapsed_ms": self.timeout.elapsed_ms,
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
                counters are snapshotted under each tracker's lock first.
        """
        from veronica_core.distributed import LocalBudgetBackend, _BUDGET_EPSILON

        # Check abort flag first (under its own lock).
        with self._lock:
            if self._aborted:
                return "aborted"

        # Snapshot each counter outside any combined lock to avoid holding
        # multiple locks simultaneously (lock-ordering risk).
        cost = self.budget.cost
        steps = self.steps.count
        retries = self.retries.count
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

        # Cross-process budget check -- potentially slow (Redis round-trip).
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

        result = self.timeout.check()
        if result is not None:
            emit_fn("timeout", "cancellation token signalled")
            return result

        return None
