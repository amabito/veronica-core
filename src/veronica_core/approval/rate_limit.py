"""Approval rate limiting for VERONICA Security Containment Layer.

Token-bucket rate limiter that caps approval requests to prevent
operators from being overwhelmed by automated approval spam.
"""
from __future__ import annotations

import threading
import time


# ---------------------------------------------------------------------------
# ApprovalRateLimiter
# ---------------------------------------------------------------------------


class ApprovalRateLimiter:
    """Token-bucket rate limiter for approval requests.

    Allows up to *max_per_window* approvals per *window_seconds* rolling
    window.  When the bucket is exhausted, :meth:`acquire` returns False
    until tokens refill.

    Thread-safe.

    Args:
        max_per_window: Maximum number of approvals allowed per window.
        window_seconds: Rolling window duration in seconds.
    """

    def __init__(
        self,
        max_per_window: int = 10,
        window_seconds: float = 60.0,
    ) -> None:
        if max_per_window <= 0:
            raise ValueError("max_per_window must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")

        self._max = max_per_window
        self._window = window_seconds
        self._lock = threading.Lock()
        # Track timestamps of successful acquisitions within the window
        self._timestamps: list[float] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> bool:
        """Attempt to consume one token.

        Drops timestamps older than *window_seconds* before checking
        capacity.

        Returns:
            True if a token was consumed (request allowed).
            False if the bucket is exhausted (request denied).
        """
        now = time.monotonic()
        with self._lock:
            cutoff = now - self._window
            # Evict expired timestamps
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            if len(self._timestamps) >= self._max:
                return False
            self._timestamps.append(now)
            return True

    def available_tokens(self) -> int:
        """Return the number of tokens currently available.

        Returns:
            Remaining capacity within the current window.
        """
        now = time.monotonic()
        with self._lock:
            cutoff = now - self._window
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            return max(0, self._max - len(self._timestamps))

    def reset(self) -> None:
        """Clear all recorded timestamps, fully refilling the bucket.

        Useful for testing or after an operator-acknowledged pause.
        """
        with self._lock:
            self._timestamps.clear()
