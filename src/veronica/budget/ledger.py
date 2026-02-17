"""VERONICA Budget ledger -- in-memory accounting with window keying."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from veronica.budget.policy import Scope, WindowKind


class BudgetLedger:
    """In-memory ledger tracking reserved and committed spend per window."""

    def __init__(self) -> None:
        # Key: (scope.value, scope_id, window_kind.value, window_id)
        self._committed: defaultdict[tuple[str, str, str, str], float] = defaultdict(float)
        self._reserved: defaultdict[tuple[str, str, str, str], float] = defaultdict(float)

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def window_id(window: WindowKind, ts: datetime | None = None) -> str:
        """Return a string window ID for the given window kind and timestamp.

        MINUTE -> "%Y%m%d%H%M"
        HOUR   -> "%Y%m%d%H"
        DAY    -> "%Y%m%d"
        """
        if ts is None:
            ts = datetime.now(timezone.utc)
        if window is WindowKind.MINUTE:
            return ts.strftime("%Y%m%d%H%M")
        if window is WindowKind.HOUR:
            return ts.strftime("%Y%m%d%H")
        return ts.strftime("%Y%m%d")

    def _key(
        self,
        scope: Scope,
        scope_id: str,
        window: WindowKind,
        ts: datetime | None = None,
    ) -> tuple[str, str, str, str]:
        return (scope.value, scope_id, window.value, self.window_id(window, ts))

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------

    def used(
        self,
        scope: Scope,
        scope_id: str,
        window: WindowKind,
        ts: datetime | None = None,
    ) -> float:
        """Return total spend (committed + reserved) for the current window."""
        k = self._key(scope, scope_id, window, ts)
        return self._committed[k] + self._reserved[k]

    def committed(
        self,
        scope: Scope,
        scope_id: str,
        window: WindowKind,
        ts: datetime | None = None,
    ) -> float:
        """Return only committed (confirmed) spend for the current window."""
        k = self._key(scope, scope_id, window, ts)
        return self._committed[k]

    def snapshot(
        self,
        scope: Scope,
        scope_id: str,
        window: WindowKind,
        ts: datetime | None = None,
    ) -> dict[str, Any]:
        """Return a dict suitable for event payloads."""
        k = self._key(scope, scope_id, window, ts)
        return {
            "scope": scope.value,
            "scope_id": scope_id,
            "window": window.value,
            "window_id": k[3],
            "committed_usd": self._committed[k],
            "reserved_usd": self._reserved[k],
            "used_usd": self._committed[k] + self._reserved[k],
        }

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def reserve(
        self,
        scope: Scope,
        scope_id: str,
        window: WindowKind,
        amount_usd: float,
        ts: datetime | None = None,
    ) -> None:
        """Add a reservation (pre-charge) for the current window."""
        k = self._key(scope, scope_id, window, ts)
        self._reserved[k] += amount_usd

    def commit(
        self,
        scope: Scope,
        scope_id: str,
        window: WindowKind,
        reserved_usd: float,
        actual_usd: float,
        ts: datetime | None = None,
    ) -> None:
        """Move reserved_usd from reserved to committed, applying actual_usd."""
        k = self._key(scope, scope_id, window, ts)
        self._reserved[k] = max(0.0, self._reserved[k] - reserved_usd)
        self._committed[k] += actual_usd

    def release(
        self,
        scope: Scope,
        scope_id: str,
        window: WindowKind,
        amount_usd: float,
        ts: datetime | None = None,
    ) -> None:
        """Remove a reservation without committing (e.g. on call failure)."""
        k = self._key(scope, scope_id, window, ts)
        self._reserved[k] = max(0.0, self._reserved[k] - amount_usd)
