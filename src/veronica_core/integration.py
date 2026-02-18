"""VERONICA Integration Helper - Bridge between state machine and existing code.

This module provides a drop-in replacement for global variables
(veronica_fail_counts, veronica_cooldowns) with state machine persistence.
"""

from __future__ import annotations
from typing import Dict, Optional
import logging
import time
import atexit

from veronica_core.state import VeronicaStateMachine, VeronicaState
from veronica_core.persist import VeronicaPersistence
from veronica_core.exit import VeronicaExit
from veronica_core.backends import PersistenceBackend, JSONBackend
from veronica_core.guards import VeronicaGuard, PermissiveGuard
from veronica_core.clients import LLMClient, NullClient
from veronica_core.shield.config import ShieldConfig
from veronica_core.shield.pipeline import ShieldPipeline

logger = logging.getLogger(__name__)


class VeronicaIntegration:
    """Integration helper for VERONICA state machine.

    Provides backward-compatible interface to replace global variables
    while adding persistence and graceful exit.
    """

    def __init__(
        self,
        cooldown_fails: int = 3,
        cooldown_seconds: int = 600,
        auto_save_interval: int = 100,  # Save every N operations
        backend: Optional[PersistenceBackend] = None,
        guard: Optional[VeronicaGuard] = None,
        client: Optional[LLMClient] = None,
        shield: Optional[ShieldConfig] = None,
    ):
        """Initialize integration layer.

        Args:
            cooldown_fails: Number of consecutive fails to trigger cooldown
            cooldown_seconds: Cooldown duration in seconds
            auto_save_interval: Save state every N operations (0 = manual only)
            backend: Persistence backend (default: JSONBackend with VeronicaPersistence path)
            guard: Validation guard (default: PermissiveGuard)
            client: LLM client (optional, default: NullClient - no LLM features)
            shield: Shield configuration (optional, default: None - no shield)
        """
        # Set up backend (backward compatible with VeronicaPersistence)
        if backend is None:
            # Default: Use VeronicaPersistence for backward compatibility
            self.persistence = VeronicaPersistence()
            self.backend = None  # Legacy mode
        else:
            self.backend = backend
            self.persistence = None  # Modern mode

        # Set up guard
        self.guard = guard or PermissiveGuard()

        # Set up LLM client (optional)
        self.client: LLMClient = client or NullClient()

        # Shield configuration (opt-in, stored only -- no behavior change yet)
        self.shield: Optional[ShieldConfig] = shield

        # Shield pipeline (created when config present; noop hooks = always ALLOW)
        self._shield_pipeline: Optional[ShieldPipeline] = (
            ShieldPipeline() if shield is not None else None
        )

        # Load state
        if self.backend:
            state_data = self.backend.load()
        else:
            # Legacy VeronicaPersistence
            loaded_state = self.persistence.load()
            state_data = loaded_state.to_dict() if loaded_state else None

        self.state = VeronicaStateMachine.from_dict(state_data) if state_data else None

        loaded_from_disk = self.state is not None

        if self.state is None:
            # Fresh start
            logger.info("[VERONICA_INTEGRATION] Creating fresh state")
            self.state = VeronicaStateMachine(
                cooldown_fails=cooldown_fails,
                cooldown_seconds=cooldown_seconds,
            )
        else:
            logger.info(
                f"[VERONICA_INTEGRATION] Loaded existing state: "
                f"{len(self.state.cooldowns)} active cooldowns, "
                f"{len(self.state.fail_counts)} fail counters"
            )

        # Transition to SCREENING (only if fresh start or not in critical state)
        if not loaded_from_disk:
            # Fresh start - always transition to SCREENING
            self.state.transition(VeronicaState.SCREENING, "Bot startup")
        else:
            # Loaded existing state - preserve critical states
            if self.state.current_state not in (VeronicaState.SAFE_MODE, VeronicaState.ERROR):
                self.state.transition(VeronicaState.SCREENING, "Bot startup resumed")
            else:
                logger.warning(
                    f"[VERONICA_INTEGRATION] Preserving critical state: {self.state.current_state.value}"
                )

        # Register exit handler
        self.exit_handler = VeronicaExit(self.state, self.persistence)

        # Auto-save tracking
        self.auto_save_interval = auto_save_interval
        self.operation_count = 0

        logger.info(
            f"[VERONICA_INTEGRATION] Initialized: "
            f"cooldown_fails={self.state.cooldown_fails}, "
            f"cooldown_seconds={self.state.cooldown_seconds}, "
            f"auto_save_interval={auto_save_interval}"
        )

    def is_in_cooldown(self, pair: str) -> bool:
        """Check if pair is in cooldown.

        Args:
            pair: Trading pair (e.g., 'btc_jpy')

        Returns:
            True if pair is in cooldown
        """
        return self.state.is_in_cooldown(pair)

    def record_fail(self, pair: str, context: Optional[Dict] = None) -> bool:
        """Record sanity_fail for pair.

        Args:
            pair: Trading pair (or entity identifier)
            context: Optional context for guard validation

        Returns:
            True if cooldown was activated
        """
        activated = self.state.record_fail(pair)

        # Check guard for early cooldown activation
        if self.guard and context:
            if self.guard.should_cooldown(pair, context):
                logger.info(f"[VERONICA_INTEGRATION] Guard triggered cooldown for {pair}")
                self.state.cooldowns[pair] = time.time() + self.state.cooldown_seconds
                self.guard.on_cooldown_activated(pair, context)
                activated = True

        self._maybe_auto_save()
        return activated

    def record_pass(self, pair: str) -> None:
        """Record sanity_pass for pair (resets fail counter).

        Args:
            pair: Trading pair
        """
        self.state.record_pass(pair)
        self._maybe_auto_save()

    def cleanup_expired(self) -> None:
        """Cleanup expired cooldowns (call periodically)."""
        expired = self.state.cleanup_expired()
        if expired:
            self._maybe_auto_save()

    def get_stats(self) -> Dict:
        """Get current state statistics."""
        return self.state.get_stats()

    def get_fail_count(self, pair: str) -> int:
        """Get current fail count for pair (for logging/debugging)."""
        return self.state.fail_counts.get(pair, 0)

    def get_cooldown_remaining(self, pair: str) -> Optional[float]:
        """Get remaining cooldown seconds for pair.

        Returns:
            Remaining seconds, or None if not in cooldown
        """
        if pair not in self.state.cooldowns:
            return None
        remaining = self.state.cooldowns[pair] - time.time()
        return max(0, remaining)

    def save(self) -> bool:
        """Manually save state.

        Returns:
            True on success
        """
        state_data = self.state.to_dict()

        # Validate via guard
        if self.guard and not self.guard.validate_state(state_data):
            logger.warning("[VERONICA_INTEGRATION] State validation failed, aborting save")
            return False

        # Save via backend or legacy persistence
        if self.backend:
            return self.backend.save(state_data)
        else:
            return self.persistence.save(self.state)

    def _maybe_auto_save(self) -> None:
        """Auto-save if interval reached."""
        if self.auto_save_interval <= 0:
            return  # Auto-save disabled

        self.operation_count += 1
        if self.operation_count >= self.auto_save_interval:
            logger.debug(
                f"[VERONICA_INTEGRATION] Auto-save triggered "
                f"(operations={self.operation_count})"
            )
            self.save()  # Use save() method (includes guard validation)
            self.operation_count = 0


# Global singleton instance (initialized in run_multi_pair_trading.py)
_veronica_integration: Optional[VeronicaIntegration] = None


def get_veronica_integration(
    cooldown_fails: int = 3,
    cooldown_seconds: int = 600,
    auto_save_interval: int = 100,
) -> VeronicaIntegration:
    """Get or create global VeronicaIntegration instance.

    Args:
        cooldown_fails: Number of consecutive fails to trigger cooldown
        cooldown_seconds: Cooldown duration in seconds
        auto_save_interval: Save state every N operations

    Returns:
        Global VeronicaIntegration instance
    """
    global _veronica_integration
    if _veronica_integration is None:
        _veronica_integration = VeronicaIntegration(
            cooldown_fails=cooldown_fails,
            cooldown_seconds=cooldown_seconds,
            auto_save_interval=auto_save_interval,
        )
    return _veronica_integration
