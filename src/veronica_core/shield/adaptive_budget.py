"""AdaptiveBudgetHook for VERONICA Execution Shield.

v0.6.0: Auto-adjusts budget ceiling based on recent SafetyEvent history.

Observes past events in a rolling window and tightens or loosens the
effective ceiling:
  - >= tighten_trigger budget-exceeded events in window -> ceiling * (1 - tighten_pct)
  - Zero DEGRADE events in window -> ceiling * (1 + loosen_pct)
  - All adjustments clamped to +/- max_adjustment of original ceiling

Records ADAPTIVE_ADJUSTMENT SafetyEvent on each non-hold adjustment.

Design principles:
  - Decoupled: does NOT wrap a hook; returns adjusted values for caller
  - Thread-safe: all state behind a lock
  - Deterministic: time can be injected for testing
  - Clamped: ceiling_multiplier always in [1 - max_adjustment, 1 + max_adjustment]
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision, ToolCallContext

_DEFAULT_TIGHTEN_EVENT_TYPES = frozenset({
    "BUDGET_EXCEEDED",
    "BUDGET_WINDOW_EXCEEDED",
    "TOKEN_BUDGET_EXCEEDED",
})

_DEFAULT_DEGRADE_EVENT_TYPES = frozenset({
    "BUDGET_WINDOW_EXCEEDED",
    "TOKEN_BUDGET_EXCEEDED",
})


@dataclass(frozen=True)
class AdjustmentResult:
    """Result of an adaptive adjustment cycle."""

    action: str  # "tighten", "loosen", "hold"
    adjusted_ceiling: int
    ceiling_multiplier: float
    base_ceiling: int
    tighten_events_in_window: int
    degrade_events_in_window: int


class AdaptiveBudgetHook:
    """Auto-adjusts budget ceiling based on recent SafetyEvent history.

    Thread-safe.  Feed events via ``feed_event()`` / ``feed_events()``,
    then call ``adjust()`` to evaluate and apply the adjustment.

    Rules:
      - >= tighten_trigger HALT events in window  -> tighten by tighten_pct
      - 0 DEGRADE events in window                -> loosen by loosen_pct
      - Otherwise                                  -> hold (no change)
      - Multiplier clamped to [1 - max_adjustment, 1 + max_adjustment]
    """

    def __init__(
        self,
        base_ceiling: int,
        window_seconds: float = 1800.0,
        tighten_trigger: int = 3,
        tighten_pct: float = 0.10,
        loosen_pct: float = 0.05,
        max_adjustment: float = 0.20,
        tighten_event_types: frozenset[str] | None = None,
        degrade_event_types: frozenset[str] | None = None,
    ) -> None:
        if base_ceiling <= 0:
            raise ValueError(
                f"base_ceiling must be positive, got {base_ceiling}"
            )
        if not (0 < tighten_pct <= 1.0):
            raise ValueError(
                f"tighten_pct must be in (0, 1.0], got {tighten_pct}"
            )
        if not (0 < loosen_pct <= 1.0):
            raise ValueError(
                f"loosen_pct must be in (0, 1.0], got {loosen_pct}"
            )
        if not (0 < max_adjustment <= 1.0):
            raise ValueError(
                f"max_adjustment must be in (0, 1.0], got {max_adjustment}"
            )

        self._base_ceiling = base_ceiling
        self._window_seconds = window_seconds
        self._tighten_trigger = tighten_trigger
        self._tighten_pct = tighten_pct
        self._loosen_pct = loosen_pct
        self._max_adjustment = max_adjustment
        self._tighten_event_types = (
            tighten_event_types or _DEFAULT_TIGHTEN_EVENT_TYPES
        )
        self._degrade_event_types = (
            degrade_event_types or _DEFAULT_DEGRADE_EVENT_TYPES
        )

        self._ceiling_multiplier: float = 1.0
        self._event_buffer: deque[tuple[float, SafetyEvent]] = deque()
        self._safety_events: list[SafetyEvent] = []
        self._lock = threading.Lock()

    # -- Properties ----------------------------------------------------------

    @property
    def base_ceiling(self) -> int:
        return self._base_ceiling

    @property
    def ceiling_multiplier(self) -> float:
        with self._lock:
            return self._ceiling_multiplier

    @property
    def adjusted_ceiling(self) -> int:
        with self._lock:
            return max(1, round(self._base_ceiling * self._ceiling_multiplier))

    @property
    def window_seconds(self) -> float:
        return self._window_seconds

    # -- Event ingestion -----------------------------------------------------

    def feed_event(
        self, event: SafetyEvent, ts: float | None = None
    ) -> None:
        """Feed a SafetyEvent for tracking.

        Args:
            event: The SafetyEvent to track.
            ts: Optional epoch-seconds override (for deterministic testing).
        """
        if ts is None:
            ts = time.time()
        with self._lock:
            self._event_buffer.append((ts, event))

    def feed_events(self, events: list[SafetyEvent]) -> None:
        """Feed multiple SafetyEvents (all stamped at current time)."""
        now = time.time()
        with self._lock:
            for event in events:
                self._event_buffer.append((now, event))

    # -- Adjustment ----------------------------------------------------------

    def adjust(
        self,
        ctx: ToolCallContext | None = None,
        *,
        _now: float | None = None,
    ) -> AdjustmentResult:
        """Analyze recent events and compute adjusted ceiling.

        The ceiling_multiplier is updated in-place.  An
        ADAPTIVE_ADJUSTMENT SafetyEvent is recorded for tighten/loosen.

        Args:
            ctx: Optional context for the SafetyEvent request_id.
            _now: Injected timestamp for deterministic testing.

        Returns:
            AdjustmentResult describing the action taken.
        """
        now = _now if _now is not None else time.time()
        cutoff = now - self._window_seconds

        with self._lock:
            # Prune expired events
            while self._event_buffer and self._event_buffer[0][0] <= cutoff:
                self._event_buffer.popleft()

            # Count relevant events
            tighten_count = 0
            degrade_count = 0
            for _, event in self._event_buffer:
                if (
                    event.event_type in self._tighten_event_types
                    and event.decision == Decision.HALT
                ):
                    tighten_count += 1
                if (
                    event.event_type in self._degrade_event_types
                    and event.decision == Decision.DEGRADE
                ):
                    degrade_count += 1

            # Compute bounds
            min_mult = 1.0 - self._max_adjustment
            max_mult = 1.0 + self._max_adjustment
            old_multiplier = self._ceiling_multiplier

            # Apply rule
            if tighten_count >= self._tighten_trigger:
                self._ceiling_multiplier = max(
                    min_mult,
                    self._ceiling_multiplier - self._tighten_pct,
                )
                action = "tighten"
            elif degrade_count == 0:
                self._ceiling_multiplier = min(
                    max_mult,
                    self._ceiling_multiplier + self._loosen_pct,
                )
                action = "loosen"
            else:
                action = "hold"

            adjusted = max(1, round(self._base_ceiling * self._ceiling_multiplier))

            result = AdjustmentResult(
                action=action,
                adjusted_ceiling=adjusted,
                ceiling_multiplier=round(self._ceiling_multiplier, 4),
                base_ceiling=self._base_ceiling,
                tighten_events_in_window=tighten_count,
                degrade_events_in_window=degrade_count,
            )

            # Record event for non-hold
            if action != "hold":
                request_id = ctx.request_id if ctx else None
                self._safety_events.append(
                    SafetyEvent(
                        event_type="ADAPTIVE_ADJUSTMENT",
                        decision=(
                            Decision.DEGRADE
                            if action == "tighten"
                            else Decision.ALLOW
                        ),
                        reason=(
                            f"{action}: multiplier "
                            f"{old_multiplier:.4f} -> "
                            f"{self._ceiling_multiplier:.4f}, "
                            f"ceiling {self._base_ceiling} -> {adjusted}, "
                            f"exceeded={tighten_count} degrade={degrade_count}"
                        ),
                        hook="AdaptiveBudgetHook",
                        request_id=request_id,
                        metadata={
                            "action": action,
                            "old_multiplier": round(old_multiplier, 4),
                            "new_multiplier": round(
                                self._ceiling_multiplier, 4
                            ),
                            "adjusted_ceiling": adjusted,
                            "base_ceiling": self._base_ceiling,
                            "tighten_events": tighten_count,
                            "degrade_events": degrade_count,
                        },
                    )
                )

            return result

    # -- Event access --------------------------------------------------------

    def get_events(self) -> list[SafetyEvent]:
        """Return accumulated ADAPTIVE_ADJUSTMENT events (shallow copy)."""
        with self._lock:
            return list(self._safety_events)

    def clear_events(self) -> None:
        """Clear accumulated ADAPTIVE_ADJUSTMENT events."""
        with self._lock:
            self._safety_events.clear()

    def reset(self) -> None:
        """Reset multiplier to 1.0 and clear all state."""
        with self._lock:
            self._ceiling_multiplier = 1.0
            self._event_buffer.clear()
            self._safety_events.clear()
