"""BudgetWindowHook for VERONICA Execution Shield.

Enforces a rolling time-window call-count ceiling with optional DEGRADE
support.  When the count within ``window_seconds`` reaches
``degrade_threshold * max_calls``, the hook returns ``Decision.DEGRADE``
(signal for model fallback).  At ``max_calls`` it returns ``Decision.HALT``.

MVP scope: call-count based only.  USD-based budgets require provider price
tables and are intentionally out of scope (scope creep risk).
"""

from __future__ import annotations

import threading
import time
from collections import deque

from veronica_core.shield.types import Decision, ToolCallContext

__all__ = ["BudgetWindowHook"]


class BudgetWindowHook:
    """Rolling time-window call-count limiter with optional DEGRADE zone.

    Thread-safe.  Internally tracks invocation timestamps in a deque;
    entries older than ``window_seconds`` are pruned on every call.

    Decision logic (after pruning expired entries):
      - count < degrade_threshold * max_calls  -> None  (ALLOW)
      - count >= degrade_threshold * max_calls
        AND count < max_calls                  -> DEGRADE
      - count >= max_calls                     -> HALT
    """

    def __init__(
        self,
        max_calls: int,
        window_seconds: float = 60.0,
        degrade_threshold: float = 0.8,
    ) -> None:
        self._max_calls = max_calls
        self._window_seconds = window_seconds
        self._degrade_threshold = degrade_threshold
        # H3: Use a generous maxlen (max_calls * 10) instead of the old
        # max_calls * 2, which was too small for burst traffic — deque eviction
        # silently dropped timestamps and undercounted active calls.  We keep a
        # finite cap to bound memory under sustained overload (e.g. long windows
        # with very high call rates).  Timestamps are also pruned explicitly in
        # before_llm_call() as entries age past the window cutoff.
        self._max_deque_size = max(max_calls * 10, 1000)
        self._timestamps: deque[float] = deque(maxlen=self._max_deque_size)
        self._lock = threading.Lock()

    def before_llm_call(self, ctx: ToolCallContext) -> Decision | None:
        """Return DEGRADE or HALT when approaching or at the call limit."""
        now = time.time()
        cutoff = now - self._window_seconds

        with self._lock:
            # Prune expired timestamps
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()

            count = len(self._timestamps)

            if count >= self._max_calls:
                # L4: Timestamp is appended even on HALT to keep the window full
                # during overload, ensuring subsequent calls also see the correct
                # count without needing to re-prune. This is intentional behavior.
                self._timestamps.append(now)
                return Decision.HALT

            degrade_at = self._degrade_threshold * self._max_calls
            if count >= degrade_at:
                self._timestamps.append(now)
                return Decision.DEGRADE

            self._timestamps.append(now)
            return None
