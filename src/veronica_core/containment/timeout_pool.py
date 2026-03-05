"""SharedTimeoutPool — shared daemon-thread timeout scheduler.

Replaces per-context threading.Thread with a single shared heap-based
scheduler.  One daemon thread wakes up at the earliest deadline and fires
callbacks; cancellation is O(1) via a cancelled-set.

Module-level singleton ``_timeout_pool`` is used by ExecutionContext when
available; falls back to legacy threading.Thread when the pool raises.

Thread safety
-------------
* ``_lock`` protects ``_heap`` and ``_cancelled``.
* ``_wakeup`` is an ``Event`` used to interrupt the daemon thread when a
  new earliest deadline is scheduled before the current sleep expires.
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

# Handle type (int counter) returned by schedule() and accepted by cancel().
_Handle = int


class SharedTimeoutPool:
    """Single daemon thread + heapq priority queue for timeout callbacks.

    Usage::

        pool = SharedTimeoutPool()
        handle = pool.schedule(deadline=time.monotonic() + 5.0, callback=my_fn)
        # ... later ...
        pool.cancel(handle)

    The pool starts the daemon thread lazily on first schedule() call.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._heap: list[tuple[float, int, Callable[[], None]]] = []
        self._cancelled: set[int] = set()
        self._counter: int = 0
        self._thread: threading.Thread | None = None
        self._started: bool = False
        self._wakeup = threading.Event()
        self._shutdown = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def schedule(self, deadline: float, callback: Callable[[], None]) -> _Handle:
        """Schedule *callback* to run at or after *deadline* (monotonic seconds).

        Args:
            deadline: Absolute monotonic time (``time.monotonic() + offset``).
            callback: Zero-argument callable to invoke.

        Returns:
            Handle that can be passed to cancel().
        """
        with self._lock:
            if self._shutdown:
                raise RuntimeError("SharedTimeoutPool has been shut down")
            self._counter += 1
            handle = self._counter
            heapq.heappush(self._heap, (deadline, handle, callback))
            # Start daemon thread lazily on first use.
            if not self._started:
                self._thread = threading.Thread(
                    target=self._run,
                    daemon=True,
                    name="veronica-timeout-pool",
                )
                self._thread.start()
                self._started = True
        # Interrupt sleeping thread so it recalculates earliest deadline.
        self._wakeup.set()
        return handle

    def cancel(self, handle: _Handle) -> None:
        """Cancel a previously scheduled callback.

        Idempotent — safe to call multiple times or after the callback
        has already fired (in which case cancel() is a no-op).

        Args:
            handle: Value returned by schedule().
        """
        with self._lock:
            self._cancelled.add(handle)

    def shutdown(self) -> None:
        """Stop the daemon thread.  Primarily for testing.

        Pending callbacks are discarded. The heap and cancelled-handle set are
        cleared to prevent unbounded memory growth across repeated test cycles.
        """
        with self._lock:
            self._shutdown = True
            self._heap.clear()
            self._cancelled.clear()
        self._wakeup.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Daemon thread loop.  Fires callbacks at their scheduled deadlines."""
        while True:
            now = time.monotonic()

            with self._lock:
                if self._shutdown:
                    return

                # Drain expired (or cancelled) entries from heap head.
                fired: list[Callable[[], None]] = []
                while self._heap:
                    deadline, handle, callback = self._heap[0]
                    if deadline <= now:
                        heapq.heappop(self._heap)
                        if handle not in self._cancelled:
                            fired.append(callback)
                        else:
                            self._cancelled.discard(handle)
                    else:
                        break

                # Determine how long to sleep until next deadline.
                if self._heap:
                    next_deadline = self._heap[0][0]
                    sleep_s = max(0.0, next_deadline - now)
                else:
                    # No pending work; sleep indefinitely until woken.
                    sleep_s = None  # type: ignore[assignment]

            # Fire callbacks outside the lock to avoid re-entrant deadlocks.
            for cb in fired:
                try:
                    cb()
                except Exception:
                    logger.debug(
                        "SharedTimeoutPool: callback raised, ignoring",
                        exc_info=True,
                    )

            # Sleep until next deadline or until woken by schedule()/cancel().
            self._wakeup.clear()
            self._wakeup.wait(timeout=sleep_s)

    @classmethod
    def instance(cls) -> "SharedTimeoutPool":
        """Return the module-level singleton SharedTimeoutPool."""
        return _timeout_pool


# Module-level singleton.  Created once; daemon thread starts on first use.
_timeout_pool = SharedTimeoutPool()
