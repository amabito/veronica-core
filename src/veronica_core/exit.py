"""VERONICA Exit Handler - Graceful shutdown with state preservation."""

from __future__ import annotations
import signal
import atexit
import logging
import threading
import warnings
from enum import IntEnum
from typing import Any, Optional

from veronica_core.state import VeronicaStateMachine, VeronicaState
from veronica_core.backends import PersistenceBackend

logger = logging.getLogger(__name__)


def _wrap_legacy_persistence(backend: Any) -> PersistenceBackend:
    """Adapt a legacy VeronicaPersistence object to the PersistenceBackend protocol.

    VeronicaPersistence.save(state_machine) accepted a VeronicaStateMachine and
    called to_dict() internally.  PersistenceBackend.save(data) accepts a plain
    dict.  This adapter bridges the two so callers passing old objects receive a
    DeprecationWarning rather than a silent TypeError at shutdown.
    """
    warnings.warn(
        "Passing a VeronicaPersistence instance is deprecated and will be removed "
        "in v2.0. Use a PersistenceBackend (e.g. JSONBackend) instead.",
        DeprecationWarning,
        stacklevel=3,
    )

    class _LegacyAdapter(PersistenceBackend):
        def save(self, data: dict) -> bool:
            # Legacy API expected a VeronicaStateMachine; create a thin proxy.
            class _DictProxy:
                def to_dict(self) -> dict:
                    return data

            return backend.save(_DictProxy())  # type: ignore[arg-type]

        def load(self) -> Optional[dict]:
            sm = backend.load()
            if sm is None:
                return None
            return sm.to_dict() if hasattr(sm, "to_dict") else sm

    return _LegacyAdapter()


class ExitTier(IntEnum):
    """Exit priority levels."""
    GRACEFUL = 1   # Save state, cleanup, log
    EMERGENCY = 2  # Save state, minimal cleanup
    FORCE = 3      # Immediate exit (no save)


class VeronicaExit:
    """Exit handler for VERONICA state persistence."""

    def __init__(
        self,
        state_machine: VeronicaStateMachine,
        persistence: Optional[Any] = None,
    ):
        self.state_machine = state_machine
        if persistence is None:
            from veronica_core.backends import JSONBackend
            self.persistence: PersistenceBackend = JSONBackend(
                "data/state/veronica_state.json"
            )
        elif isinstance(persistence, PersistenceBackend):
            self.persistence = persistence
        else:
            # Legacy VeronicaPersistence or any object with save(state_machine).
            self.persistence = _wrap_legacy_persistence(persistence)
        self.exit_requested = False
        self.exit_tier: Optional[ExitTier] = None
        self.exit_reason: str = ""
        self._exit_lock = threading.Lock()

        # Register handlers
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register signal handlers and atexit."""
        # Graceful shutdown on SIGTERM/SIGINT
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # Atexit as fallback
        atexit.register(self._atexit_handler)

        logger.info("[VERONICA_EXIT] Exit handlers registered (SIGTERM, SIGINT, atexit)")

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle SIGTERM/SIGINT."""
        signal_name = signal.Signals(signum).name
        logger.warning(f"[VERONICA_EXIT] Signal received: {signal_name}")

        if signum == signal.SIGTERM:
            self.request_exit(ExitTier.GRACEFUL, "SIGTERM received")
        elif signum == signal.SIGINT:
            self.request_exit(ExitTier.EMERGENCY, "SIGINT (Ctrl+C) received")

    def _atexit_handler(self) -> None:
        """Atexit fallback (process termination)."""
        if not self.exit_requested:
            logger.warning("[VERONICA_EXIT] atexit triggered without explicit exit request")
            self.request_exit(ExitTier.EMERGENCY, "atexit fallback")

    def request_exit(self, tier: ExitTier, reason: str) -> None:
        """Request exit at specified tier. Thread-safe against duplicate signals."""
        with self._exit_lock:
            if self.exit_requested:
                logger.warning(f"[VERONICA_EXIT] Exit already requested (tier={self.exit_tier}), ignoring")
                return

            self.exit_requested = True
            self.exit_tier = tier
            self.exit_reason = reason

        logger.warning(
            f"[VERONICA_EXIT] Exit requested: tier={tier.name} reason='{reason}'"
        )

        # Execute tier-specific logic
        if tier == ExitTier.GRACEFUL:
            self._graceful_exit()
        elif tier == ExitTier.EMERGENCY:
            self._emergency_exit()
        elif tier == ExitTier.FORCE:
            self._force_exit()

    def _graceful_exit(self) -> None:
        """Tier 1: Full cleanup with state save."""
        logger.info("[VERONICA_EXIT] GRACEFUL exit initiated")

        # 1. Transition to SAFE_MODE
        self.state_machine.transition(VeronicaState.SAFE_MODE, self.exit_reason)

        # 2. Cleanup expired cooldowns
        expired = self.state_machine.cleanup_expired()
        if expired:
            logger.info(f"[VERONICA_EXIT] Cleaned up {len(expired)} expired cooldowns")

        # 3. Save state
        success = self.persistence.save(self.state_machine.to_dict())
        if success:
            logger.info("[VERONICA_EXIT] State saved successfully")
        else:
            logger.error("[VERONICA_EXIT] State save FAILED")

        # 4. Log final stats
        stats = self.state_machine.get_stats()
        logger.info(f"[VERONICA_EXIT] Final stats: {stats}")

        logger.info("[VERONICA_EXIT] GRACEFUL exit complete")

    def _emergency_exit(self) -> None:
        """Tier 2: Minimal cleanup, save state."""
        logger.warning("[VERONICA_EXIT] EMERGENCY exit initiated")

        # 1. Transition to SAFE_MODE
        self.state_machine.transition(VeronicaState.SAFE_MODE, self.exit_reason)

        # 2. Save state (critical)
        success = self.persistence.save(self.state_machine.to_dict())
        if success:
            logger.info("[VERONICA_EXIT] Emergency state save OK")
        else:
            logger.error("[VERONICA_EXIT] Emergency state save FAILED")

        logger.warning("[VERONICA_EXIT] EMERGENCY exit complete")

    def _force_exit(self) -> None:
        """Tier 3: Immediate exit, no save."""
        logger.error("[VERONICA_EXIT] FORCE exit initiated (NO STATE SAVE)")
        logger.error(f"[VERONICA_EXIT] Reason: {self.exit_reason}")
        # No save, no cleanup

    def is_exit_requested(self) -> bool:
        """Check if exit has been requested."""
        return self.exit_requested
