"""Recovery orchestrator for VERONICA self-healing containment.

Coordinates IntegrityMonitor, CheckpointManager, and SentinelMonitor
into a single on_call() hook that wraps each LLM invocation.
Fail-closed: unrecoverable state returns QUARANTINE_ALL.

Decision logic on each on_call():
  1. If already quarantined, return QUARANTINE_ALL immediately.
  2. If sentinel timeout detected, quarantine and return QUARANTINE_ALL.
  3. Run IntegrityMonitor.on_call().
  4. If TAMPERED or QUARANTINED: attempt checkpoint restore.
     - No valid checkpoint -> QUARANTINE_ALL.
     - Restore signature invalid -> QUARANTINE_ALL.
     - Restore succeeded -> return RESTORED.
  5. Periodically capture a new checkpoint (every checkpoint_interval calls).
  6. Return CONTINUE.
"""

from __future__ import annotations

import threading
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from veronica_core.recovery.checkpoint import CheckpointManager
    from veronica_core.recovery.integrity import IntegrityMonitor
    from veronica_core.recovery.sentinel import SentinelMonitor


class RecoveryAction(str, Enum):
    """Action returned by RecoveryOrchestrator.on_call()."""

    CONTINUE = "continue"  # All subsystems clean
    RESTORED = "restored"  # Tamper detected, restored from checkpoint
    QUARANTINE_ALL = "quarantine_all"  # Unrecoverable -- block everything


class RecoveryOrchestrator:
    """Coordinates integrity monitoring, checkpointing, and sentinel.

    Called once per wrap_llm_call invocation via on_call().
    Periodically captures new checkpoints (every checkpoint_interval calls)
    when an ExecutionContext is provided.

    Thread-safe: orchestration state protected by threading.Lock.
    """

    def __init__(
        self,
        integrity: "IntegrityMonitor",
        checkpoint_mgr: "CheckpointManager",
        sentinel: "SentinelMonitor | None" = None,
        checkpoint_interval: int = 50,
    ) -> None:
        if checkpoint_interval < 1:
            raise ValueError("checkpoint_interval must be >= 1")
        self._integrity = integrity
        self._checkpoint_mgr = checkpoint_mgr
        self._sentinel = sentinel
        self._checkpoint_interval = checkpoint_interval
        self._call_count: int = 0
        self._quarantined: bool = False
        self._lock = threading.Lock()

    def on_call(self, ctx: Any | None = None) -> RecoveryAction:
        """Called on each wrap_llm_call. Orchestrates all recovery checks.

        Args:
            ctx: ExecutionContext or any object with containment state.
                 Used for periodic checkpoint capture. May be None.

        Returns:
            RecoveryAction indicating CONTINUE, RESTORED, or QUARANTINE_ALL.
        """
        # Deferred imports to avoid circular import at module load time
        from veronica_core.recovery.checkpoint import RestoreResult
        from veronica_core.recovery.integrity import IntegrityVerdict

        with self._lock:
            if self._quarantined:
                return RecoveryAction.QUARANTINE_ALL
            self._call_count += 1
            call_count = self._call_count
            checkpoint_interval = self._checkpoint_interval

        # Step 1: Sentinel timeout is a hard fail
        if self._sentinel is not None and self._sentinel.check_timeout():
            with self._lock:
                self._quarantined = True
            return RecoveryAction.QUARANTINE_ALL

        # Step 2: Integrity check
        verdict = self._integrity.on_call()

        if verdict in (IntegrityVerdict.TAMPERED, IntegrityVerdict.QUARANTINED):
            # Attempt restore from most recent valid checkpoint
            latest = self._checkpoint_mgr.latest_valid()
            if latest is None:
                with self._lock:
                    self._quarantined = True
                return RecoveryAction.QUARANTINE_ALL

            result = self._checkpoint_mgr.restore(latest)
            if result == RestoreResult.SUCCESS:
                return RecoveryAction.RESTORED

            with self._lock:
                self._quarantined = True
            return RecoveryAction.QUARANTINE_ALL

        # Step 3: Periodic checkpoint capture (non-fatal on failure)
        if ctx is not None and call_count % checkpoint_interval == 0:
            try:
                self._checkpoint_mgr.capture(ctx)
            except Exception:
                pass

        return RecoveryAction.CONTINUE

    @property
    def is_healthy(self) -> bool:
        """True if all subsystems report clean (no quarantine active)."""
        with self._lock:
            if self._quarantined:
                return False
        if self._integrity.is_quarantined:
            return False
        if self._sentinel is not None and self._sentinel.check_timeout():
            return False
        return True

    @property
    def call_count(self) -> int:
        """Total calls processed."""
        with self._lock:
            return self._call_count
