"""TimeAwarePolicy for VERONICA Execution Shield.

v0.6.0: Multiplies budget ceilings based on time-of-day and day-of-week.

Weekend and off-hours get reduced budget ceilings (configurable multipliers).
Records TIME_POLICY_APPLIED SafetyEvent when a multiplier is active.

Design principles:
  - Pure function: given a datetime, returns the multiplier
  - No side effects on other hooks; caller applies the multiplier
  - Thread-safe
  - Deterministic: accepts injected datetime for testing
"""

from __future__ import annotations

import threading
from datetime import datetime, time, timezone

from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision, ToolCallContext


class TimeAwarePolicy:
    """Computes budget multiplier based on time-of-day and day-of-week.

    Thread-safe.  Call ``evaluate()`` to get the current multiplier and
    optional SafetyEvent.

    Schedule:
      - Weekend (Saturday/Sunday): weekend_multiplier
      - Off-hours (before work_start or after work_end): offhour_multiplier
      - Both weekend AND off-hours: min(weekend, offhour)
      - Business hours on weekdays: 1.0 (no adjustment)

    The multiplier is intended to be applied to a budget ceiling, e.g.::

        effective_ceiling = int(base_ceiling * policy.evaluate(ctx).multiplier)
    """

    def __init__(
        self,
        weekend_multiplier: float = 0.85,
        offhour_multiplier: float = 0.90,
        work_start: time = time(9, 0),
        work_end: time = time(18, 0),
        tz: timezone | None = None,
    ) -> None:
        if not (0 < weekend_multiplier <= 1.0):
            raise ValueError(
                f"weekend_multiplier must be in (0, 1.0], got {weekend_multiplier}"
            )
        if not (0 < offhour_multiplier <= 1.0):
            raise ValueError(
                f"offhour_multiplier must be in (0, 1.0], got {offhour_multiplier}"
            )
        if work_start >= work_end:
            raise ValueError(
                f"work_start ({work_start}) must be before work_end ({work_end})"
            )

        self._weekend_multiplier = weekend_multiplier
        self._offhour_multiplier = offhour_multiplier
        self._work_start = work_start
        self._work_end = work_end
        self._tz = tz or timezone.utc
        self._safety_events: list[SafetyEvent] = []
        self._lock = threading.Lock()

    # -- Properties ----------------------------------------------------------

    @property
    def weekend_multiplier(self) -> float:
        return self._weekend_multiplier

    @property
    def offhour_multiplier(self) -> float:
        return self._offhour_multiplier

    @property
    def work_start(self) -> time:
        return self._work_start

    @property
    def work_end(self) -> time:
        return self._work_end

    # -- Evaluation ----------------------------------------------------------

    def get_multiplier(self, dt: datetime | None = None) -> float:
        """Return the budget multiplier for the given datetime.

        Args:
            dt: Datetime to evaluate.  Defaults to now in configured tz.

        Returns:
            Float in (0, 1.0].  1.0 = no reduction (business hours).
        """
        if dt is None:
            dt = datetime.now(self._tz)

        is_weekend = dt.weekday() >= 5  # Saturday=5, Sunday=6
        current_time = dt.time()
        is_offhour = current_time < self._work_start or current_time >= self._work_end

        if is_weekend and is_offhour:
            return min(self._weekend_multiplier, self._offhour_multiplier)
        if is_weekend:
            return self._weekend_multiplier
        if is_offhour:
            return self._offhour_multiplier
        return 1.0

    def evaluate(
        self,
        ctx: ToolCallContext | None = None,
        *,
        dt: datetime | None = None,
    ) -> TimeResult:
        """Evaluate policy and record SafetyEvent if multiplier < 1.0.

        Args:
            ctx: Optional context for SafetyEvent request_id.
            dt: Injected datetime for deterministic testing.

        Returns:
            TimeResult with multiplier and classification.
        """
        if dt is None:
            dt = datetime.now(self._tz)

        multiplier = self.get_multiplier(dt)
        is_weekend = dt.weekday() >= 5
        current_time = dt.time()
        is_offhour = current_time < self._work_start or current_time >= self._work_end

        if is_weekend and is_offhour:
            classification = "weekend_offhour"
        elif is_weekend:
            classification = "weekend"
        elif is_offhour:
            classification = "offhour"
        else:
            classification = "business_hours"

        result = TimeResult(
            multiplier=multiplier,
            classification=classification,
            is_weekend=is_weekend,
            is_offhour=is_offhour,
        )

        # Record event when multiplier reduces the budget
        if multiplier < 1.0:
            request_id = ctx.request_id if ctx else None
            with self._lock:
                self._safety_events.append(
                    SafetyEvent(
                        event_type="TIME_POLICY_APPLIED",
                        decision=Decision.DEGRADE,
                        reason=(
                            f"{classification}: multiplier={multiplier:.2f}, "
                            f"time={current_time.isoformat()}, "
                            f"weekday={dt.weekday()}"
                        ),
                        hook="TimeAwarePolicy",
                        request_id=request_id,
                        metadata={
                            "multiplier": multiplier,
                            "classification": classification,
                            "is_weekend": is_weekend,
                            "is_offhour": is_offhour,
                            "time": current_time.isoformat(),
                            "weekday": dt.weekday(),
                        },
                    )
                )

        return result

    # -- Event access --------------------------------------------------------

    def get_events(self) -> list[SafetyEvent]:
        """Return accumulated TIME_POLICY_APPLIED events (shallow copy)."""
        with self._lock:
            return list(self._safety_events)

    def clear_events(self) -> None:
        """Clear accumulated events."""
        with self._lock:
            self._safety_events.clear()


class TimeResult:
    """Result of a time-aware policy evaluation."""

    __slots__ = ("multiplier", "classification", "is_weekend", "is_offhour")

    def __init__(
        self,
        multiplier: float,
        classification: str,
        is_weekend: bool,
        is_offhour: bool,
    ) -> None:
        self.multiplier = multiplier
        self.classification = classification
        self.is_weekend = is_weekend
        self.is_offhour = is_offhour
