"""VERONICA State Machine - Failsafe for trading bot.

Manages state transitions, cooldown, and fail counter logic.
Replaces global variables with proper state management.
"""

from __future__ import annotations
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Optional, List
import time
import logging

logger = logging.getLogger(__name__)


class VeronicaState(Enum):
    """VERONICA operational states."""
    IDLE = "IDLE"           # No active trading
    SCREENING = "SCREENING" # Active market screening
    COOLDOWN = "COOLDOWN"   # Pair-specific cooldown active
    SAFE_MODE = "SAFE_MODE" # Emergency safe mode (all trading halted)
    ERROR = "ERROR"         # System error state


@dataclass
class StateTransition:
    """Record of state transition."""
    from_state: VeronicaState
    to_state: VeronicaState
    timestamp: float
    reason: str


@dataclass
class VeronicaStateMachine:
    """VERONICA state machine with cooldown and fail tracking."""

    # Configuration
    cooldown_fails: int = 3
    cooldown_seconds: int = 600  # 10 minutes

    # State tracking (per-pair)
    fail_counts: Dict[str, int] = field(default_factory=dict)
    cooldowns: Dict[str, float] = field(default_factory=dict)  # {pair: expiry_timestamp}

    # Global state
    current_state: VeronicaState = VeronicaState.IDLE
    state_history: List[StateTransition] = field(default_factory=list)

    def is_in_cooldown(self, pair: str) -> bool:
        """Check if pair is in cooldown."""
        now = time.time()
        if pair in self.cooldowns:
            if now < self.cooldowns[pair]:
                return True
            else:
                # Cooldown expired - cleanup
                self.cleanup_expired_pair(pair)
        return False

    def record_fail(self, pair: str) -> bool:
        """Record sanity_fail for pair. Returns True if cooldown activated."""
        self.fail_counts[pair] = self.fail_counts.get(pair, 0) + 1

        if self.fail_counts[pair] >= self.cooldown_fails:
            # Activate cooldown
            self.cooldowns[pair] = time.time() + self.cooldown_seconds
            logger.warning(
                f"[VERONICA_STATE] {pair} cooldown activated: "
                f"{self.cooldown_fails} consecutive fails, "
                f"expires in {self.cooldown_seconds}s"
            )
            return True

        return False

    def record_pass(self, pair: str) -> None:
        """Record sanity_pass for pair. Resets fail counter."""
        if pair in self.fail_counts:
            logger.info(f"[VERONICA_STATE] {pair} fail counter reset (was {self.fail_counts[pair]})")
            self.fail_counts.pop(pair, None)

    def cleanup_expired_pair(self, pair: str) -> None:
        """Cleanup expired cooldown for specific pair."""
        if pair in self.cooldowns:
            del self.cooldowns[pair]
            self.fail_counts.pop(pair, None)
            logger.info(f"[VERONICA_STATE] {pair} cooldown expired and cleaned up")

    def cleanup_expired(self) -> List[str]:
        """Cleanup all expired cooldowns. Returns list of cleaned pairs."""
        now = time.time()
        expired = [pair for pair, expiry in self.cooldowns.items() if now >= expiry]
        for pair in expired:
            self.cleanup_expired_pair(pair)
        return expired

    def transition(self, to_state: VeronicaState, reason: str) -> None:
        """Transition to new state with validation."""
        if to_state == self.current_state:
            return  # No-op

        # Record transition
        transition = StateTransition(
            from_state=self.current_state,
            to_state=to_state,
            timestamp=time.time(),
            reason=reason
        )
        self.state_history.append(transition)

        # Keep only last 100 transitions
        if len(self.state_history) > 100:
            self.state_history = self.state_history[-100:]

        logger.info(
            f"[VERONICA_STATE] Transition: {self.current_state.value} -> {to_state.value} ({reason})"
        )
        self.current_state = to_state

    def get_stats(self) -> Dict:
        """Get current state statistics."""
        now = time.time()
        active_cooldowns = {
            pair: max(0, expiry - now)
            for pair, expiry in self.cooldowns.items()
            if expiry > now
        }

        return {
            "current_state": self.current_state.value,
            "active_cooldowns": active_cooldowns,
            "fail_counts": dict(self.fail_counts),
            "total_transitions": len(self.state_history),
            "last_transition": (
                {
                    "from": self.state_history[-1].from_state.value,
                    "to": self.state_history[-1].to_state.value,
                    "reason": self.state_history[-1].reason,
                    "timestamp": self.state_history[-1].timestamp,
                }
                if self.state_history else None
            ),
        }

    def to_dict(self) -> Dict:
        """Serialize state to dict for persistence."""
        return {
            "cooldown_fails": self.cooldown_fails,
            "cooldown_seconds": self.cooldown_seconds,
            "fail_counts": dict(self.fail_counts),
            "cooldowns": dict(self.cooldowns),
            "current_state": self.current_state.value,
            "state_history": [
                {
                    "from_state": t.from_state.value,
                    "to_state": t.to_state.value,
                    "timestamp": t.timestamp,
                    "reason": t.reason,
                }
                for t in self.state_history[-100:]  # Last 100 only
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict) -> VeronicaStateMachine:
        """Deserialize state from dict."""
        instance = cls(
            cooldown_fails=data.get("cooldown_fails", 3),
            cooldown_seconds=data.get("cooldown_seconds", 600),
        )
        instance.fail_counts = data.get("fail_counts", {})
        instance.cooldowns = data.get("cooldowns", {})
        instance.current_state = VeronicaState(data.get("current_state", "IDLE"))
        instance.state_history = [
            StateTransition(
                from_state=VeronicaState(t["from_state"]),
                to_state=VeronicaState(t["to_state"]),
                timestamp=t["timestamp"],
                reason=t["reason"],
            )
            for t in data.get("state_history", [])
        ]
        return instance
