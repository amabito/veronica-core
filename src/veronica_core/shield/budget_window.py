"""BudgetWindowHook for VERONICA Execution Shield.

Enforces a rolling time-window call-count ceiling.  When the number of
``before_llm_call`` invocations within the last ``window_seconds`` reaches
``max_calls`` the hook returns ``Decision.HALT``.

MVP scope: call-count based only.  USD-based budgets require provider price
tables and are intentionally out of scope (scope creep risk).
"""

from __future__ import annotations

import threading
import time
from collections import deque

from veronica_core.shield.types import Decision, ToolCallContext


class BudgetWindowHook:
    """Rolling time-window call-count limiter.

    Thread-safe.  Internally tracks invocation timestamps in a deque;
    entries older than ``window_seconds`` are pruned on every call.
    """

    def __init__(self, max_calls: int, window_seconds: float = 60.0) -> None:
        self._max_calls = max_calls
        self._window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def before_llm_call(self, ctx: ToolCallContext) -> Decision | None:
        """Halt when the rolling call count meets or exceeds max_calls."""
        now = time.time()
        cutoff = now - self._window_seconds

        with self._lock:
            # Prune expired timestamps
            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()

            if len(self._timestamps) >= self._max_calls:
                return Decision.HALT

            self._timestamps.append(now)
            return None
