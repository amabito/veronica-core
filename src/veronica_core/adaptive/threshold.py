"""AdaptiveThresholdPolicy -- Predictive budget exhaustion policy.

Implements RuntimePolicy protocol.  Evaluates time-to-exhaustion from
BurnRateEstimator and escalates decisions: ALLOW → WARN → DEGRADE → HALT.

Spike detection: if instantaneous rate > spike_multiplier * EMA rate
the policy immediately returns DEGRADE.

Complementary to AdaptiveBudgetHook (reactive, past events):
this module is predictive (future projection).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Optional

from veronica_core.adaptive.burn_rate import BurnRateEstimator
from veronica_core.runtime_policy import (
    PolicyContext,
    PolicyDecision,
    deny,
)

_POLICY_TYPE = "adaptive_threshold"


@dataclass
class AdaptiveConfig:
    """Configuration thresholds for AdaptiveThresholdPolicy.

    All time values are in hours.

    Args:
        warn_at_exhaustion_hours: Issue WARN when time-to-exhaustion
            drops below this value.  Default 24h.
        degrade_at_exhaustion_hours: Issue DEGRADE below this value.
            Default 6h.
        halt_at_exhaustion_hours: Issue HALT below this value.  Default 1h.
        spike_multiplier: If instantaneous burn rate exceeds
            spike_multiplier * EMA rate → immediate DEGRADE.  Default 3.0.
    """

    warn_at_exhaustion_hours: float = 24.0
    degrade_at_exhaustion_hours: float = 6.0
    halt_at_exhaustion_hours: float = 1.0
    spike_multiplier: float = 3.0

    def __post_init__(self) -> None:
        if not (
            0
            < self.halt_at_exhaustion_hours
            <= self.degrade_at_exhaustion_hours
            <= self.warn_at_exhaustion_hours
        ):
            raise ValueError(
                "Threshold ordering must satisfy: "
                "0 < halt <= degrade <= warn, got "
                f"halt={self.halt_at_exhaustion_hours}, "
                f"degrade={self.degrade_at_exhaustion_hours}, "
                f"warn={self.warn_at_exhaustion_hours}"
            )
        if self.spike_multiplier <= 1.0:
            raise ValueError(
                f"spike_multiplier must be > 1.0, got {self.spike_multiplier}"
            )


def _degrade(reason: str) -> PolicyDecision:
    """Return an allowed DEGRADE decision with RATE_LIMIT degradation action."""
    return PolicyDecision(
        allowed=True,
        policy_type="adaptive_threshold",
        reason=reason,
        degradation_action="RATE_LIMIT",
    )


class AdaptiveThresholdPolicy:
    """Predictive budget exhaustion policy implementing RuntimePolicy.

    Evaluates the BurnRateEstimator's time_to_exhaustion() and returns
    ALLOW / WARN / DEGRADE / HALT based on AdaptiveConfig thresholds.

    WARN and DEGRADE decisions still have allowed=True (degradation).
    HALT has allowed=False (hard stop).

    Spike detection returns allowed=True with degradation_action
    "RATE_LIMIT" when instantaneous rate exceeds EMA by spike_multiplier.

    Thread-safe: remaining_budget guarded by Lock.
    """

    def __init__(
        self,
        burn_rate: BurnRateEstimator,
        remaining_budget: float,
        config: Optional[AdaptiveConfig] = None,
    ) -> None:
        self._burn_rate = burn_rate
        self._remaining_budget = remaining_budget
        self._config = config or AdaptiveConfig()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # RuntimePolicy protocol
    # ------------------------------------------------------------------

    @property
    def policy_type(self) -> str:
        return _POLICY_TYPE

    def check(self, context: PolicyContext) -> PolicyDecision:
        """Evaluate current burn rate and return a PolicyDecision.

        Decision escalation (time-to-exhaustion based):
          >= warn threshold  → ALLOW
          < warn, >= degrade → WARN  (allowed=True, reason set)
          < degrade, >= halt → DEGRADE (allowed=True, degradation_action)
          < halt             → HALT  (allowed=False)
          zero budget        → HALT  (allowed=False)

        Spike detection (takes priority over exhaustion tiers):
          instantaneous rate > spike_multiplier * EMA → DEGRADE

        Note:
            This method does NOT call ``burn_rate.record()``.  The caller
            is responsible for feeding the BurnRateEstimator separately
            so that the policy and the estimator can be composed freely.

        Args:
            context: PolicyContext (cost_usd used for remaining budget update
                     if positive).

        Returns:
            PolicyDecision reflecting the current risk level.
        """
        with self._lock:
            # Update remaining budget from context cost
            if context.cost_usd > 0.0 and math.isfinite(context.cost_usd):
                self._remaining_budget = max(
                    0.0, self._remaining_budget - context.cost_usd
                )
            remaining = self._remaining_budget

        # Immediate HALT for zero budget
        if remaining <= 0.0:
            return deny(
                policy_type=_POLICY_TYPE,
                reason="Budget exhausted: remaining_budget <= 0",
            )

        cfg = self._config

        # Spike detection
        spike_decision = self._check_spike()
        if spike_decision is not None:
            return spike_decision

        # Exhaustion tiers
        tte = self._burn_rate.time_to_exhaustion(remaining)
        if tte is None:
            return PolicyDecision(
                allowed=True,
                policy_type=_POLICY_TYPE,
                reason="No burn rate detected",
            )

        tte_hours = tte / 3600.0

        if tte_hours < cfg.halt_at_exhaustion_hours:
            return deny(
                policy_type=_POLICY_TYPE,
                reason=(
                    f"HALT: time to exhaustion {tte_hours:.2f}h"
                    f" < {cfg.halt_at_exhaustion_hours:.1f}h threshold"
                ),
            )

        if tte_hours < cfg.degrade_at_exhaustion_hours:
            return _degrade(
                f"DEGRADE: time to exhaustion {tte_hours:.2f}h"
                f" < {cfg.degrade_at_exhaustion_hours:.1f}h threshold"
            )

        if tte_hours < cfg.warn_at_exhaustion_hours:
            return PolicyDecision(
                allowed=True,
                policy_type=_POLICY_TYPE,
                reason=(
                    f"WARN: time to exhaustion {tte_hours:.2f}h"
                    f" < {cfg.warn_at_exhaustion_hours:.1f}h threshold"
                ),
            )

        return PolicyDecision(
            allowed=True,
            policy_type=_POLICY_TYPE,
            reason="ALLOW",
        )

    def reset(self) -> None:
        """Reset policy state."""
        # No internal counters to reset beyond remaining_budget,
        # which is intentionally preserved across reset() calls.
        pass

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def update_remaining_budget(self, remaining: float) -> None:
        """Externally update remaining budget (e.g. after budget refill).

        Args:
            remaining: New remaining budget value.
        """
        with self._lock:
            self._remaining_budget = max(0.0, remaining)

    @property
    def remaining_budget(self) -> float:
        """Current remaining budget snapshot."""
        with self._lock:
            return self._remaining_budget

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_spike(self) -> Optional[PolicyDecision]:
        """Return a DEGRADE decision if a rate spike is detected, else None."""
        cfg = self._config
        # Atomic snapshot: single lock acquisition for both windows.
        instant_rate, baseline_rate = self._burn_rate.current_rates(
            windows_sec=[60.0, 3600.0]
        )

        if baseline_rate <= 0.0 or instant_rate <= 0.0:
            return None

        if instant_rate > cfg.spike_multiplier * baseline_rate:
            return _degrade(
                f"SPIKE: instantaneous rate {instant_rate:.4f}/s"
                f" > {cfg.spike_multiplier:.1f}x baseline {baseline_rate:.4f}/s"
            )
        return None
