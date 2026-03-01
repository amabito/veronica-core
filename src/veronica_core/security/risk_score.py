"""Risk Score accumulator and SAFE_MODE auto-transition for VERONICA Security."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from veronica_core.shield.types import Decision, ToolCallContext

if TYPE_CHECKING:
    from veronica_core.security.capabilities import CapabilitySet
    from veronica_core.security.policy_engine import PolicyEngine, PolicyHook
    from veronica_core.shield.pipeline import ShieldPipeline


# ---------------------------------------------------------------------------
# RiskScoreConfig
# ---------------------------------------------------------------------------


@dataclass
class RiskScoreConfig:
    """Configuration for risk score accumulation and SAFE_MODE transition."""

    deny_threshold: int = 20
    window_size: int = 100
    reset_on_allow: bool = False


# ---------------------------------------------------------------------------
# RiskScoreAccumulator
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    """A single decision record."""

    delta: int
    verdict: str


class RiskScoreAccumulator:
    """Thread-safe accumulator of risk score deltas.

    When ``current_score`` reaches or exceeds ``config.deny_threshold``,
    ``is_safe_mode`` becomes True.  Call ``reset()`` to clear the state.
    """

    def __init__(self, config: RiskScoreConfig | None = None) -> None:
        self._config = config or RiskScoreConfig()
        self._lock = threading.Lock()
        self._window: list[_Entry] = []

    def add(self, delta: int, verdict: str) -> None:
        """Record a decision delta.  Maintains a sliding window of size ``window_size``."""
        with self._lock:
            self._window.append(_Entry(delta=delta, verdict=verdict))
            # Trim to window_size
            max_size = self._config.window_size
            if len(self._window) > max_size:
                self._window = self._window[-max_size:]
            # Optionally reset on ALLOW
            if self._config.reset_on_allow and verdict == "ALLOW":
                self._window.clear()

    @property
    def current_score(self) -> int:
        """Sum of risk_score_delta values in the current window."""
        with self._lock:
            return sum(e.delta for e in self._window)

    @property
    def is_safe_mode(self) -> bool:
        """Return True when cumulative score has reached the deny threshold.

        Both the score computation and the threshold comparison are performed
        under a single lock acquisition to prevent a TOCTOU race where another
        thread adds a high-delta entry between the score read and the
        comparison.
        """
        with self._lock:
            return sum(e.delta for e in self._window) >= self._config.deny_threshold

    def reset(self) -> None:
        """Clear all accumulated entries and reset score to zero."""
        with self._lock:
            self._window.clear()


# ---------------------------------------------------------------------------
# RiskAwareHook
# ---------------------------------------------------------------------------

_DEFAULT_DENY_DELTA = 10


class RiskAwareHook:
    """Wraps a PolicyHook and enforces SAFE_MODE via RiskScoreAccumulator.

    Implements the PreDispatchHook protocol.

    Priority order:
    1. If accumulator.is_safe_mode → HALT immediately (no inner call).
    2. Otherwise delegate to inner PolicyHook.
    3. If inner returns HALT → accumulate risk_score_delta from last_decision.
    """

    def __init__(
        self,
        inner: "PolicyHook",
        accumulator: RiskScoreAccumulator,
    ) -> None:
        self._inner = inner
        self._accumulator = accumulator

    def _delegate(self, ctx: ToolCallContext, *, llm: bool) -> Decision | None:
        """Shared logic for before_llm_call and before_tool_call."""
        # SAFE_MODE check takes absolute priority
        if self._accumulator.is_safe_mode:
            return Decision.HALT

        if llm:
            # PolicyHook does not implement before_llm_call; treat as ALLOW
            inner_result: Decision | None = Decision.ALLOW
        else:
            inner_result = self._inner.before_tool_call(ctx)

        if inner_result == Decision.HALT:
            last = self._inner.last_decision
            delta = last.risk_score_delta if last is not None else _DEFAULT_DENY_DELTA
            self._accumulator.add(delta, "DENY")

        return inner_result

    def before_llm_call(self, ctx: ToolCallContext) -> Decision | None:
        """Intercept LLM calls: SAFE_MODE → HALT, else delegate."""
        return self._delegate(ctx, llm=True)

    def before_tool_call(self, ctx: ToolCallContext) -> Decision | None:
        """Intercept tool calls: SAFE_MODE → HALT, else delegate."""
        return self._delegate(ctx, llm=False)


# ---------------------------------------------------------------------------
# RiskAwareShieldFactory
# ---------------------------------------------------------------------------


class RiskAwareShieldFactory:
    """Factory that wires PolicyHook → RiskAwareHook → ShieldPipeline."""

    @staticmethod
    def create(
        policy_engine: "PolicyEngine",
        caps: "CapabilitySet",
        repo_root: str | Path,
        risk_config: RiskScoreConfig | None = None,
    ) -> "tuple[ShieldPipeline, RiskScoreAccumulator]":
        """Create a wired ShieldPipeline with risk score tracking.

        Returns:
            (pipeline, accumulator) — callers can check accumulator.is_safe_mode.
        """
        from veronica_core.security.policy_engine import PolicyHook
        from veronica_core.shield.pipeline import ShieldPipeline

        accumulator = RiskScoreAccumulator(config=risk_config)

        hook = PolicyHook(
            engine=policy_engine,
            caps=caps,
            repo_root=str(repo_root),
        )
        risk_hook = RiskAwareHook(inner=hook, accumulator=accumulator)

        # Wire as tool_dispatch so pipeline.before_tool_call routes through RiskAwareHook.
        # Also wire as pre_dispatch so pipeline.before_llm_call is covered.
        pipeline = ShieldPipeline(pre_dispatch=risk_hook, tool_dispatch=risk_hook)
        return pipeline, accumulator
