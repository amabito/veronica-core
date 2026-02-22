"""Degradation Ladder: multi-tier graceful degradation before HALT.

Tiers (in order of activation as cost rises):
  ALLOW -> MODEL_DOWNGRADE -> CONTEXT_TRIM -> RATE_LIMIT -> HALT

Usage:
    from veronica_core.shield.degradation import DegradationLadder, DegradationConfig

    ladder = DegradationLadder(DegradationConfig(
        model_map={"gpt-4o": "gpt-4o-mini"},
        rate_limit_ms=2000,
        cost_thresholds={"model_downgrade": 0.80, "context_trim": 0.85, "rate_limit": 0.90},
    ))

    decision = ladder.evaluate(cost_accumulated=0.85, max_cost_usd=1.0, current_model="gpt-4o")
    if decision and decision.degradation_action == "MODEL_DOWNGRADE":
        model = decision.fallback_model  # use this model instead
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from veronica_core.runtime_policy import PolicyDecision

logger = logging.getLogger(__name__)


@runtime_checkable
class Trimmer(Protocol):
    """Protocol for context trimmer implementations."""

    def trim(self, messages: list) -> list:
        """Trim a list of messages to reduce context size."""
        ...


class NoOpTrimmer:
    """Trimmer that returns messages unchanged."""

    def trim(self, messages: list) -> list:
        return messages


@dataclass
class DegradationConfig:
    """Configuration for the DegradationLadder.

    Attributes:
        model_map: Mapping from expensive model name to cheaper fallback.
        rate_limit_ms: Delay in milliseconds applied at RATE_LIMIT tier.
        cost_thresholds: Fraction-of-max-cost triggers for each tier.
        trimmer: Trimmer implementation for CONTEXT_TRIM tier.
    """

    model_map: dict[str, str] = field(default_factory=dict)
    rate_limit_ms: int = 1000
    cost_thresholds: dict[str, float] = field(
        default_factory=lambda: {
            "model_downgrade": 0.80,
            "context_trim": 0.85,
            "rate_limit": 0.90,
        }
    )
    trimmer: Trimmer = field(default_factory=NoOpTrimmer)


class DegradationLadder:
    """Evaluates which degradation tier is appropriate based on cost fraction.

    Checks tiers from highest (RATE_LIMIT) to lowest (MODEL_DOWNGRADE) so
    that higher-severity tiers take precedence when multiple thresholds are met.
    """

    def __init__(self, config: DegradationConfig | None = None) -> None:
        self._config = config or DegradationConfig()

    def evaluate(
        self,
        cost_accumulated: float,
        max_cost_usd: float,
        current_model: str = "",
    ) -> PolicyDecision | None:
        """Return degradation decision, or None if below all thresholds.

        Args:
            cost_accumulated: Total cost spent so far (USD).
            max_cost_usd: Budget ceiling (USD). Must be > 0.
            current_model: Model identifier for MODEL_DOWNGRADE lookup.

        Returns:
            PolicyDecision with degradation_action set, or None.
        """
        if max_cost_usd <= 0:
            return None

        fraction = cost_accumulated / max_cost_usd
        thresholds = self._config.cost_thresholds

        rate_limit_threshold = thresholds.get("rate_limit", 0.90)
        context_trim_threshold = thresholds.get("context_trim", 0.85)
        model_downgrade_threshold = thresholds.get("model_downgrade", 0.80)

        if fraction >= rate_limit_threshold:
            from veronica_core.runtime_policy import rate_limit_decision

            logger.debug("DegradationLadder: RATE_LIMIT at %.1f%%", fraction * 100)
            return rate_limit_decision(
                delay_ms=self._config.rate_limit_ms,
                reason=f"cost at {fraction:.0%} of ceiling; rate limiting",
            )

        if fraction >= context_trim_threshold:
            from veronica_core.runtime_policy import PolicyDecision

            logger.debug("DegradationLadder: CONTEXT_TRIM at %.1f%%", fraction * 100)
            return PolicyDecision(
                allowed=True,
                policy_type="context_trim",
                reason=f"cost at {fraction:.0%} of ceiling; context trim recommended",
                degradation_action="CONTEXT_TRIM",
            )

        if fraction >= model_downgrade_threshold:
            fallback = self._config.model_map.get(current_model, "")
            if fallback:
                from veronica_core.runtime_policy import model_downgrade

                logger.debug(
                    "DegradationLadder: MODEL_DOWNGRADE %r->%r at %.1f%%",
                    current_model,
                    fallback,
                    fraction * 100,
                )
                return model_downgrade(
                    current_model=current_model,
                    fallback_model=fallback,
                    reason=f"cost at {fraction:.0%} of ceiling; downgrading model",
                )

        return None

    def apply_rate_limit(self, decision: PolicyDecision) -> None:
        """Block for the rate limit delay specified in the decision."""
        if hasattr(decision, "rate_limit_ms") and decision.rate_limit_ms > 0:
            time.sleep(decision.rate_limit_ms / 1000.0)

    def apply_context_trim(self, messages: list) -> list:
        """Apply the configured trimmer to the message list."""
        return self._config.trimmer.trim(messages)
