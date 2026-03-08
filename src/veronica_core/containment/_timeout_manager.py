"""Internal timeout manager for ExecutionContext.

_TimeoutManager wraps a CancellationToken and the shared timeout pool.
It tracks elapsed time and manages the scheduled watcher callback.

This module is package-internal (_-prefix); do NOT import it from outside
veronica_core.containment.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from veronica_core.containment.types import CancellationToken

logger = logging.getLogger(__name__)


class TimeoutManager:
    """Thread-safe timeout tracking with CancellationToken integration.

    Owns:
        - A reference to the chain-level CancellationToken.
        - The monotonic start time (write-once at construction).
        - An optional handle into the shared timeout pool.

    The caller is responsible for calling cancel_watcher() during cleanup to
    release the pool handle and avoid spurious callbacks.
    """

    def __init__(self, token: "CancellationToken") -> None:
        self._token = token
        self._start_time: float = time.monotonic()
        self._lock = threading.Lock()
        self._pool_handle: Any = None

    @property
    def elapsed_ms(self) -> float:
        """Return milliseconds elapsed since construction (monotonic clock)."""
        # _start_time is write-once; no lock needed for the read.
        return (time.monotonic() - self._start_time) * 1000.0

    def check(self) -> str | None:
        """Return "timeout" if the cancellation token is already signalled.

        Returns:
            "timeout" when is_cancelled is True; None otherwise.
        """
        if self._token.is_cancelled:
            return "timeout"
        return None

    def start_watcher(
        self,
        timeout_ms: int,
        emit_fn: Any,
        config_timeout_ms: int,
    ) -> None:
        """Schedule a cancellation callback via the shared timeout pool.

        Args:
            timeout_ms: Milliseconds until timeout fires.
            emit_fn: Callable(stop_reason, detail) used to emit a chain event
                when the timeout fires.
            config_timeout_ms: The configured timeout_ms value, used for the
                event detail message.
        """
        from veronica_core.containment.timeout_pool import _timeout_pool

        timeout_s = timeout_ms / 1000.0
        deadline = time.monotonic() + timeout_s
        token = self._token  # Capture for closure; avoids holding self._lock.

        def _on_timeout() -> None:
            if not token.is_cancelled:
                try:
                    emit_fn(
                        "timeout",
                        f"timeout_ms={config_timeout_ms} elapsed",
                    )
                finally:
                    token.cancel()

        with self._lock:
            old_handle = self._pool_handle
            self._pool_handle = _timeout_pool.schedule(deadline, _on_timeout)

        # Cancel previous handle if start_watcher() was called twice.
        if old_handle is not None:
            try:
                _timeout_pool.cancel(old_handle)
            except Exception:
                pass

    def cancel_watcher(self) -> None:
        """Cancel the scheduled timeout callback (if any).

        Idempotent and exception-safe. Should be called during context cleanup.
        """
        from veronica_core.containment.timeout_pool import _timeout_pool

        with self._lock:
            handle = self._pool_handle
            self._pool_handle = None

        if handle is not None:
            try:
                _timeout_pool.cancel(handle)
            except Exception:
                # Intentionally swallowed: cancel is best-effort; the callback
                # fires harmlessly if the context is already marked aborted.
                pass
