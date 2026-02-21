"""VERONICA Integration Helper - Bridge between state machine and existing code.

This module provides a drop-in replacement for global variables
(veronica_fail_counts, veronica_cooldowns) with state machine persistence.
"""

from __future__ import annotations
from typing import Dict, Optional
import logging
import threading
import time
import atexit

from veronica_core.state import VeronicaStateMachine, VeronicaState
from veronica_core.persist import VeronicaPersistence
from veronica_core.exit import VeronicaExit
from veronica_core.backends import PersistenceBackend, JSONBackend
from veronica_core.guards import VeronicaGuard, PermissiveGuard
from veronica_core.clients import LLMClient, NullClient
from veronica_core.shield.budget_window import BudgetWindowHook
from veronica_core.shield.config import ShieldConfig
from veronica_core.shield.input_compression import InputCompressionHook
from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.safe_mode import SafeModeHook
from veronica_core.shield.token_budget import TokenBudgetHook
from veronica_core.policies.minimal_response import MinimalResponsePolicy

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

        # Shield configuration (opt-in)
        self.shield: Optional[ShieldConfig] = shield

        # Shield pipeline (created when config present)
        if shield is not None:
            safe_hook = SafeModeHook(enabled=True) if shield.safe_mode.enabled else None
            # safe_mode takes priority: when enabled it HALTs everything,
            # so there is no point also running BudgetWindowHook.
            if safe_hook is not None:
                pre_dispatch_hook = safe_hook
            elif shield.token_budget.enabled:
                pre_dispatch_hook = TokenBudgetHook(
                    max_output_tokens=shield.token_budget.max_output_tokens,
                    max_total_tokens=shield.token_budget.max_total_tokens,
                    degrade_threshold=shield.token_budget.degrade_threshold,
                )
            elif shield.budget_window.enabled:
                pre_dispatch_hook = BudgetWindowHook(
                    max_calls=shield.budget_window.max_calls,
                    window_seconds=shield.budget_window.window_seconds,
                    degrade_threshold=shield.budget_window.degrade_threshold,
                )
            else:
                pre_dispatch_hook = None
            self._shield_pipeline: Optional[ShieldPipeline] = ShieldPipeline(
                pre_dispatch=pre_dispatch_hook,
                retry=safe_hook,
            )
            # InputCompressionHook is NOT a pre-dispatch hook.
            # It requires the actual prompt text, so callers invoke
            # check_input() directly.  We store the instance here for
            # convenient access via the integration object.
            if shield.input_compression.enabled:
                self._input_compression_hook: Optional[InputCompressionHook] = (
                    InputCompressionHook(
                        compression_threshold_tokens=shield.input_compression.compression_threshold_tokens,
                        halt_threshold_tokens=shield.input_compression.halt_threshold_tokens,
                        fallback_to_original=shield.input_compression.fallback_to_original,
                    )
                )
            else:
                self._input_compression_hook = None
        else:
            self._shield_pipeline = None
            self._input_compression_hook = None

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

        # When using modern backend mode, also register atexit to save via backend
        if self.backend is not None:
            atexit.register(self.save)

        # Auto-save tracking
        self.auto_save_interval = auto_save_interval
        self.operation_count = 0
        self._op_lock = threading.Lock()

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

        self._op_lock.acquire()
        self.operation_count += 1
        should_save = self.operation_count >= self.auto_save_interval
        if should_save:
            self.operation_count = 0
        self._op_lock.release()

        if should_save:
            logger.debug(
                f"[VERONICA_INTEGRATION] Auto-save triggered"
            )
            self.save()  # Use save() method (includes guard validation)


# Global singleton instance (initialized in run_multi_pair_trading.py)
_veronica_integration: Optional[VeronicaIntegration] = None
_singleton_lock = threading.Lock()


def get_veronica_integration(
    cooldown_fails: int = 3,
    cooldown_seconds: int = 600,
    auto_save_interval: int = 100,
) -> VeronicaIntegration:
    """Get or create global VeronicaIntegration instance.

    Thread-safe: double-checked locking prevents duplicate initialization.

    Args:
        cooldown_fails: Number of consecutive fails to trigger cooldown
        cooldown_seconds: Cooldown duration in seconds
        auto_save_interval: Save state every N operations

    Returns:
        Global VeronicaIntegration instance
    """
    global _veronica_integration
    if _veronica_integration is None:
        with _singleton_lock:
            if _veronica_integration is None:
                _veronica_integration = VeronicaIntegration(
                    cooldown_fails=cooldown_fails,
                    cooldown_seconds=cooldown_seconds,
                    auto_save_interval=auto_save_interval,
                )
    return _veronica_integration
