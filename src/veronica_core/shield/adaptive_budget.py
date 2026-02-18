"""AdaptiveBudgetHook for VERONICA Execution Shield.

v0.6.0: Auto-adjusts budget ceiling based on recent SafetyEvent history.
v0.7.0: Cooldown window, adjustment smoothing, hard floor/ceiling.

Observes past events in a rolling window and tightens or loosens the
effective ceiling:
  - >= tighten_trigger budget-exceeded events in window -> ceiling * (1 - tighten_pct)
  - Zero DEGRADE events in window -> ceiling * (1 + loosen_pct)

Stabilization (v0.7.0):
  - Cooldown: minimum interval between adjustments (cooldown_seconds)
  - Smoothing: per-step cap on multiplier change (max_step_pct)
  - Floor/Ceiling: absolute bounds on multiplier (min_multiplier, max_multiplier)

Records ADAPTIVE_ADJUSTMENT SafetyEvent on each non-hold adjustment.
Records ADAPTIVE_COOLDOWN_BLOCKED SafetyEvent when cooldown prevents adjustment.

Design principles:
  - Decoupled: does NOT wrap a hook; returns adjusted values for caller
  - Thread-safe: all state behind a lock
  - Deterministic: time can be injected for testing
  - Backward compatible: new params default to no-op values
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
        # v0.7.0 stabilization (defaults are backward-compatible)
        cooldown_seconds: float = 0.0,
        max_step_pct: float = 1.0,
        min_multiplier: float | None = None,
        max_multiplier: float | None = None,
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
        if cooldown_seconds < 0:
            raise ValueError(
                f"cooldown_seconds must be >= 0, got {cooldown_seconds}"
            )
        if not (0 < max_step_pct <= 1.0):
            raise ValueError(
                f"max_step_pct must be in (0, 1.0], got {max_step_pct}"
            )

        # Compute floor/ceiling from max_adjustment if not explicitly set
        resolved_min = min_multiplier if min_multiplier is not None else (1.0 - max_adjustment)
        resolved_max = max_multiplier if max_multiplier is not None else (1.0 + max_adjustment)

        if resolved_min <= 0:
            raise ValueError(
                f"min_multiplier must be > 0, got {resolved_min}"
            )
        if resolved_min >= resolved_max:
            raise ValueError(
                f"min_multiplier ({resolved_min}) must be < "
                f"max_multiplier ({resolved_max})"
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

        # v0.7.0 stabilization
        self._cooldown_seconds = cooldown_seconds
        self._max_step_pct = max_step_pct
        self._min_multiplier = resolved_min
        self._max_multiplier = resolved_max

        self._ceiling_multiplier: float = 1.0
        self._last_adjustment_ts: float | None = None
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

    @property
    def cooldown_seconds(self) -> float:
        return self._cooldown_seconds

    @property
    def max_step_pct(self) -> float:
        return self._max_step_pct

    @property
    def min_multiplier(self) -> float:
        return self._min_multiplier

    @property
    def max_multiplier(self) -> float:
        return self._max_multiplier

    @property
    def last_adjustment_ts(self) -> float | None:
        with self._lock:
            return self._last_adjustment_ts

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
        An ADAPTIVE_COOLDOWN_BLOCKED SafetyEvent is recorded when the
        cooldown window prevents an adjustment.

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

            # Cooldown check (v0.7.0)
            if (
                self._cooldown_seconds > 0
                and self._last_adjustment_ts is not None
            ):
                elapsed = now - self._last_adjustment_ts
                if elapsed < self._cooldown_seconds:
                    adjusted = max(
                        1,
                        round(self._base_ceiling * self._ceiling_multiplier),
                    )
                    request_id = ctx.request_id if ctx else None
                    self._safety_events.append(
                        SafetyEvent(
                            event_type="ADAPTIVE_COOLDOWN_BLOCKED",
                            decision=Decision.DEGRADE,
                            reason=(
                                f"cooldown: {elapsed:.0f}s elapsed, "
                                f"{self._cooldown_seconds:.0f}s required"
                            ),
                            hook="AdaptiveBudgetHook",
                            request_id=request_id,
                            metadata={
                                "elapsed_seconds": round(elapsed, 1),
                                "cooldown_seconds": self._cooldown_seconds,
                                "remaining_seconds": round(
                                    self._cooldown_seconds - elapsed, 1
                                ),
                            },
                        )
                    )
                    return AdjustmentResult(
                        action="cooldown_blocked",
                        adjusted_ceiling=adjusted,
                        ceiling_multiplier=round(
                            self._ceiling_multiplier, 4
                        ),
                        base_ceiling=self._base_ceiling,
                        tighten_events_in_window=tighten_count,
                        degrade_events_in_window=degrade_count,
                    )

            old_multiplier = self._ceiling_multiplier

            # Apply rule with smoothing (v0.7.0: per-step cap)
            if tighten_count >= self._tighten_trigger:
                step = min(self._tighten_pct, self._max_step_pct)
                self._ceiling_multiplier = max(
                    self._min_multiplier,
                    self._ceiling_multiplier - step,
                )
                action = "tighten"
            elif degrade_count == 0:
                step = min(self._loosen_pct, self._max_step_pct)
                self._ceiling_multiplier = min(
                    self._max_multiplier,
                    self._ceiling_multiplier + step,
                )
                action = "loosen"
            else:
                action = "hold"

            adjusted = max(
                1, round(self._base_ceiling * self._ceiling_multiplier)
            )

            result = AdjustmentResult(
                action=action,
                adjusted_ceiling=adjusted,
                ceiling_multiplier=round(self._ceiling_multiplier, 4),
                base_ceiling=self._base_ceiling,
                tighten_events_in_window=tighten_count,
                degrade_events_in_window=degrade_count,
            )

            # Record event and update cooldown timestamp for non-hold
            if action != "hold":
                self._last_adjustment_ts = now
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
                            f"exceeded={tighten_count} "
                            f"degrade={degrade_count}"
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
            self._last_adjustment_ts = None
            self._event_buffer.clear()
            self._safety_events.clear()
