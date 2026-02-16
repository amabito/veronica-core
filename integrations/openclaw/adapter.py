"""OpenClaw + VERONICA Integration Adapter.

Wraps OpenClaw strategy engines with VERONICA's failsafe execution layer.

Usage:
    from integrations.openclaw.adapter import SafeOpenClawExecutor

    strategy = YourOpenClawStrategy()
    executor = SafeOpenClawExecutor(strategy)

    result = executor.safe_execute(context)
    if result["status"] == "success":
        handle_result(result["data"])
"""

from typing import Any, Dict, Optional
from veronica_core import VeronicaIntegration
from veronica_core.state import VeronicaState
from veronica_core.backends import PersistenceBackend
from veronica_core.guards import VeronicaGuard


class SafeOpenClawExecutor:
    """Wraps OpenClaw strategy engine with VERONICA safety layer.

    Architecture:
        OpenClaw Strategy → SafeOpenClawExecutor (this class) → VERONICA Core → External Systems

    Example:
        >>> from openclaw import Strategy
        >>> strategy = Strategy(config={...})
        >>> executor = SafeOpenClawExecutor(strategy)
        >>> result = executor.safe_execute({"market_data": {...}})
        >>> if result["status"] == "success":
        ...     execute_trade(result["data"])
    """

    def __init__(
        self,
        strategy: Any,
        cooldown_fails: int = 3,
        cooldown_seconds: int = 600,
        auto_save_interval: int = 100,
        entity_id: Optional[str] = None,
        backend: Optional[PersistenceBackend] = None,
        guard: Optional[VeronicaGuard] = None,
    ):
        """Initialize SafeOpenClawExecutor.

        Args:
            strategy: OpenClaw strategy instance
            cooldown_fails: Circuit breaker threshold (consecutive fails)
            cooldown_seconds: Cooldown duration in seconds
            auto_save_interval: Auto-save every N operations
            entity_id: Entity identifier for circuit breaker tracking
            backend: Persistence backend (default: JSONBackend)
            guard: Custom validation guard (default: PermissiveGuard)
        """
        self.strategy = strategy
        self.entity_id = entity_id or "openclaw_strategy"

        # Initialize VERONICA safety layer
        self.veronica = VeronicaIntegration(
            cooldown_fails=cooldown_fails,
            cooldown_seconds=cooldown_seconds,
            auto_save_interval=auto_save_interval,
            backend=backend,
            guard=guard,
        )

    def safe_execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute OpenClaw strategy with safety validation.

        Args:
            context: Execution context (passed to strategy.execute())

        Returns:
            Result dict with keys:
                - status: "success" | "failed" | "blocked"
                - reason: Error message (if failed/blocked)
                - data: Strategy output (if success)

        Example:
            >>> result = executor.safe_execute({"market_data": {...}})
            >>> if result["status"] == "blocked":
            ...     print(f"Blocked: {result['reason']}")
        """
        # Safety check 1: Circuit breaker
        if self.veronica.is_in_cooldown(self.entity_id):
            remaining = self.veronica.get_cooldown_remaining(self.entity_id)
            return {
                "status": "blocked",
                "reason": f"Circuit breaker active ({remaining:.0f}s remaining)",
                "data": None,
            }

        # Safety check 2: SAFE_MODE
        if self.veronica.state.current_state == VeronicaState.SAFE_MODE:
            return {
                "status": "blocked",
                "reason": "SAFE_MODE active (emergency halt)",
                "data": None,
            }

        # Execute OpenClaw strategy
        try:
            # Call strategy.execute() (OpenClaw API)
            # Note: Adjust method name if OpenClaw uses different API (e.g., .decide(), .run())
            strategy_result = self.strategy.execute(context)

            # Record success
            self.veronica.record_pass(self.entity_id)

            return {
                "status": "success",
                "reason": None,
                "data": strategy_result,
            }

        except Exception as e:
            # Record failure (may trigger circuit breaker)
            cooldown_activated = self.veronica.record_fail(self.entity_id)

            if cooldown_activated:
                # Circuit breaker just activated
                return {
                    "status": "failed",
                    "reason": f"Strategy execution failed: {str(e)} (circuit breaker activated)",
                    "data": None,
                }
            else:
                # Failure recorded, but circuit breaker not yet activated
                fail_count = self.veronica.get_fail_count(self.entity_id)
                return {
                    "status": "failed",
                    "reason": f"Strategy execution failed: {str(e)} (fail count: {fail_count})",
                    "data": None,
                }

    def trigger_safe_mode(self, reason: str):
        """Manually trigger SAFE_MODE (emergency halt).

        Args:
            reason: Human-readable reason for halt

        Example:
            >>> executor.trigger_safe_mode("Anomaly detected - halting execution")
            >>> executor.veronica.save()  # Persist state
        """
        self.veronica.state.transition(VeronicaState.SAFE_MODE, reason)
        self.veronica.save()

    def clear_safe_mode(self, reason: str = "Manual clear"):
        """Clear SAFE_MODE (allow execution to resume).

        Args:
            reason: Human-readable reason for clearing

        Example:
            >>> executor.clear_safe_mode("Investigation complete - resuming")
        """
        if self.veronica.state.current_state == VeronicaState.SAFE_MODE:
            self.veronica.state.transition(VeronicaState.IDLE, reason)
            self.veronica.save()

    def get_status(self) -> Dict[str, Any]:
        """Get current safety layer status.

        Returns:
            Status dict with keys:
                - state: Current state (IDLE/COOLDOWN/SAFE_MODE/etc.)
                - in_cooldown: Boolean
                - cooldown_remaining: Seconds (if in cooldown)
                - fail_count: Number of consecutive fails

        Example:
            >>> status = executor.get_status()
            >>> print(f"State: {status['state']}, Fails: {status['fail_count']}")
        """
        return {
            "state": self.veronica.state.current_state.value,
            "in_cooldown": self.veronica.is_in_cooldown(self.entity_id),
            "cooldown_remaining": self.veronica.get_cooldown_remaining(self.entity_id),
            "fail_count": self.veronica.get_fail_count(self.entity_id),
        }


# Convenience function for quick integration
def wrap_openclaw_strategy(
    strategy: Any,
    cooldown_fails: int = 3,
    cooldown_seconds: int = 600,
    **kwargs,
) -> SafeOpenClawExecutor:
    """Convenience function to wrap OpenClaw strategy with VERONICA.

    Args:
        strategy: OpenClaw strategy instance
        cooldown_fails: Circuit breaker threshold
        cooldown_seconds: Cooldown duration in seconds
        **kwargs: Additional SafeOpenClawExecutor parameters

    Returns:
        SafeOpenClawExecutor instance

    Example:
        >>> from openclaw import Strategy
        >>> from integrations.openclaw.adapter import wrap_openclaw_strategy
        >>> executor = wrap_openclaw_strategy(Strategy())
        >>> result = executor.safe_execute({...})
    """
    return SafeOpenClawExecutor(
        strategy,
        cooldown_fails=cooldown_fails,
        cooldown_seconds=cooldown_seconds,
        **kwargs,
    )
