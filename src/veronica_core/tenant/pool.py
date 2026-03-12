"""BudgetPool -- hierarchical budget allocation for multi-tenant management.

Provides a thread-safe pool that can be shared across multiple tenants.
Supports both in-memory (backend=None) and distributed (backend provided) modes.
"""

from __future__ import annotations

import math
import threading
from typing import TYPE_CHECKING

from veronica_core.distributed import _BUDGET_EPSILON

if TYPE_CHECKING:
    from veronica_core.distributed import BudgetBackend

__all__ = [
    "BudgetExhaustedError",
    "BudgetPool",
]


class BudgetExhaustedError(RuntimeError):
    """Raised when a budget pool has insufficient funds to fulfil a request."""


class BudgetPool:
    """A budget pool that distributes a fixed total across named children.

    Children are allocated a slice of the pool via ``allocate()``.  They can
    then call ``spend()`` to consume from their slice and ``release()`` to
    return unused allocation back to the pool.

    Thread-safety is guaranteed for all public methods.  When *backend* is
    ``None`` the pool operates entirely in-memory using ``threading.Lock``.
    When a :class:`~veronica_core.distributed.BudgetBackend` is supplied the
    ``spend()`` path delegates to ``reserve``/``commit``/``rollback`` for
    distributed consistency (the in-memory allocations are still tracked
    locally).

    Args:
        total: Total budget available to allocate across all children.
        pool_id: Optional label used in error messages and debug output.
        backend: Optional distributed backend.  When provided the backend's
            ``reserve``/``commit``/``rollback`` are called on each ``spend()``
            operation.  Pass ``None`` (default) for pure in-memory operation.
    """

    def __init__(
        self,
        total: float,
        pool_id: str = "",
        backend: "BudgetBackend | None" = None,
    ) -> None:
        if not (math.isfinite(total) and total >= 0.0):
            raise ValueError(
                f"BudgetPool total must be non-negative and finite, got {total!r}"
            )
        self._total = total
        self._pool_id = pool_id
        self._backend = backend
        # child_id -> allocated budget ceiling
        self._allocations: dict[str, float] = {}
        # child_id -> amount already spent from their allocation
        self._spent: dict[str, float] = {}
        # Running total of all active allocations (avoids O(n) sum on each allocate).
        self._total_allocated: float = 0.0
        self._lock = threading.Lock()
        # Cache reservable check at construction -- avoids repeated isinstance/import.
        self._is_reservable: bool = self._check_reservable(backend)

    @staticmethod
    def _check_reservable(backend: "BudgetBackend | None") -> bool:
        """Return True if *backend* implements ReservableBudgetBackend."""
        if backend is None:
            return False
        try:
            from veronica_core.distributed import ReservableBudgetBackend

            return isinstance(backend, ReservableBudgetBackend)
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def allocate(self, child_id: str, amount: float) -> bool:
        """Reserve *amount* of budget for *child_id*.

        Returns ``True`` if the allocation succeeded, ``False`` if the pool
        has insufficient remaining budget.  Negative, zero, NaN, or infinite
        amounts are rejected (returns ``False``).

        Re-allocating an existing child replaces the previous allocation; any
        unspent portion of the old allocation is reclaimed first.
        """
        if not (math.isfinite(amount) and amount > 0.0):
            return False
        with self._lock:
            # Reclaim existing allocation for this child before checking space.
            existing_alloc = self._allocations.get(child_id, 0.0)
            existing_spent = self._spent.get(child_id, 0.0)
            # "in use" = already spent; reclaim = allocated - spent
            reclaim = max(0.0, existing_alloc - existing_spent)
            effective_allocated = self._total_allocated - reclaim
            available = self._total - effective_allocated
            if amount > available + _BUDGET_EPSILON:
                return False
            # Commit: adjust allocation.  Existing spent is preserved.
            new_alloc = existing_spent + amount
            self._total_allocated += new_alloc - existing_alloc
            self._allocations[child_id] = new_alloc
            if child_id not in self._spent:
                self._spent[child_id] = 0.0
            return True

    def release(self, child_id: str) -> float:
        """Release *child_id*'s remaining allocation back to the pool.

        Returns the amount returned (allocation minus spent).  Idempotent:
        calling again after the allocation is already zero returns 0.0.
        """
        with self._lock:
            if child_id not in self._allocations:
                return 0.0
            alloc = self._allocations[child_id]
            spent = self._spent.get(child_id, 0.0)
            returned = max(0.0, alloc - spent)
            # Update running total before removing the entry.
            self._total_allocated -= alloc
            del self._allocations[child_id]
            self._spent.pop(child_id, None)
            return returned

    def spend(self, child_id: str, amount: float) -> bool:
        """Deduct *amount* from *child_id*'s allocation.

        Returns ``True`` on success, ``False`` if the child has no allocation
        or insufficient remaining budget.  Negative, zero, NaN, or infinite
        amounts are rejected (returns ``False``).

        When a :class:`~veronica_core.distributed.ReservableBudgetBackend` is
        configured the spend is executed as reserve → commit (or rollback on
        failure) for distributed safety.  A plain ``BudgetBackend`` receives
        an ``add()`` call instead.
        """
        if not (math.isfinite(amount) and amount > 0.0):
            return False
        with self._lock:
            if child_id not in self._allocations:
                return False
            alloc = self._allocations[child_id]
            spent = self._spent.get(child_id, 0.0)
            remaining = alloc - spent
            if amount > remaining + _BUDGET_EPSILON:
                return False
            # Tentatively record the spend.
            self._spent[child_id] = spent + amount

        # Propagate to distributed backend outside the local lock.
        if self._backend is not None:
            if not self._backend_spend(amount):
                # Rollback local spend on backend failure.
                # Guard: release() may have removed child_id between locks.
                with self._lock:
                    if child_id in self._allocations:
                        self._spent[child_id] = spent
                return False
        return True

    def usage(self) -> dict[str, float]:
        """Return a snapshot of per-child spending."""
        with self._lock:
            return dict(self._spent)

    def remaining(self) -> float:
        """Return pool-level remaining budget (total minus sum of allocations)."""
        with self._lock:
            return max(0.0, self._total - self._total_allocated)

    def remaining_for(self, child_id: str) -> float:
        """Return *child_id*'s remaining allocation (allocation minus spent)."""
        with self._lock:
            alloc = self._allocations.get(child_id, 0.0)
            spent = self._spent.get(child_id, 0.0)
            return max(0.0, alloc - spent)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def total(self) -> float:
        """Return the total budget for this pool."""
        return self._total

    @property
    def pool_id(self) -> str:
        """Return the pool identifier."""
        return self._pool_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _backend_spend(self, amount: float) -> bool:
        """Propagate a committed local spend to the backend.

        Uses cached ``_is_reservable`` flag to avoid per-call isinstance checks.

        Returns:
            True on success, False on failure.
        """
        if self._is_reservable:
            # Two-phase: reserve then commit; rollback on commit failure.
            try:
                rid = self._backend.reserve(amount, self._total)  # type: ignore[union-attr]
                try:
                    self._backend.commit(rid)  # type: ignore[union-attr]
                except Exception:
                    try:
                        self._backend.rollback(rid)  # type: ignore[union-attr]
                    except Exception:
                        # Intentionally swallowed: rollback is best-effort
                        # cleanup after a commit failure; we still return False
                        # to signal the spend() call did not succeed.
                        pass
                    return False
            except Exception:
                return False
            return True
        # Plain BudgetBackend: just add.
        try:
            self._backend.add(amount)  # type: ignore[union-attr]
        except Exception:
            return False
        return True
