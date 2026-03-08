"""Tests for TimeoutManager.

Covers initial state, cancellation detection, elapsed time, and watcher lifecycle.
"""

from __future__ import annotations

import time

from veronica_core.containment._timeout_manager import TimeoutManager
from veronica_core.containment.types import CancellationToken


class TestTimeoutManagerBasic:
    def test_initial_elapsed_is_non_negative(self) -> None:
        token = CancellationToken()
        mgr = TimeoutManager(token)
        assert mgr.elapsed_ms >= 0.0

    def test_elapsed_ms_monotonic(self) -> None:
        token = CancellationToken()
        mgr = TimeoutManager(token)
        t1 = mgr.elapsed_ms
        time.sleep(0.05)
        t2 = mgr.elapsed_ms
        # Allow 1 ms tolerance to account for clock resolution on busy CI machines.
        assert t2 >= t1

    def test_check_not_cancelled(self) -> None:
        token = CancellationToken()
        mgr = TimeoutManager(token)
        assert mgr.check() is None

    def test_check_cancelled(self) -> None:
        token = CancellationToken()
        mgr = TimeoutManager(token)
        token.cancel()
        assert mgr.check() == "timeout"

    def test_cancel_watcher_without_start_is_safe(self) -> None:
        """cancel_watcher() must not raise when no watcher was started."""
        token = CancellationToken()
        mgr = TimeoutManager(token)
        # Should not raise.
        mgr.cancel_watcher()

    def test_start_watcher_fires_callback(self) -> None:
        """A short watcher should fire and cancel the token."""
        token = CancellationToken()
        mgr = TimeoutManager(token)
        fired: list[bool] = []

        def emit_fn(stop_reason: str, detail: str) -> None:
            fired.append(True)

        mgr.start_watcher(timeout_ms=50, emit_fn=emit_fn, config_timeout_ms=50)
        # Wait up to 500 ms for the token to be cancelled.
        deadline = time.monotonic() + 0.5
        while not token.is_cancelled and time.monotonic() < deadline:
            time.sleep(0.01)

        mgr.cancel_watcher()
        assert token.is_cancelled
        assert fired

    def test_cancel_watcher_prevents_double_fire(self) -> None:
        """Cancelling watcher before timeout fires must prevent token cancellation."""
        token = CancellationToken()
        mgr = TimeoutManager(token)
        fired: list[bool] = []

        def emit_fn(stop_reason: str, detail: str) -> None:
            fired.append(True)

        # Schedule with a long timeout; cancel immediately.
        mgr.start_watcher(timeout_ms=5000, emit_fn=emit_fn, config_timeout_ms=5000)
        mgr.cancel_watcher()
        # Token must NOT be cancelled.
        time.sleep(0.05)
        assert not token.is_cancelled
        assert not fired
