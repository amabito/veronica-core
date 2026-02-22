"""TokenBudgetHook for VERONICA Execution Shield.

Enforces cumulative token budget with optional DEGRADE support.
When output tokens reach degrade_threshold * max_output_tokens,
returns Decision.DEGRADE. At max_output_tokens, returns Decision.HALT.

MVP: caller reports usage via record_usage(). No automatic counting.
"""

from __future__ import annotations

import threading

from veronica_core.shield.types import Decision, ToolCallContext


class TokenBudgetHook:
    """Cumulative token budget limiter with optional DEGRADE zone.

    Thread-safe. Caller must report usage via record_usage() after each call.
    The hook checks accumulated totals in before_llm_call().

    Decision logic:
      - output_total >= max_output_tokens          -> HALT
      - output_total >= degrade_threshold * max_out -> DEGRADE
      - total_total >= max_total_tokens (if set)    -> HALT
      - total_total >= degrade_threshold * max_total -> DEGRADE (if set)
      - otherwise                                   -> None (ALLOW)
    """

    def __init__(
        self,
        max_output_tokens: int,
        max_total_tokens: int = 0,  # 0 = disabled
        degrade_threshold: float = 0.8,
    ) -> None:
        self._max_output_tokens = max_output_tokens
        self._max_total_tokens = max_total_tokens
        self._degrade_threshold = degrade_threshold
        self._output_total: int = 0
        self._input_total: int = 0
        self._lock = threading.Lock()

    @property
    def output_total(self) -> int:
        with self._lock:
            return self._output_total

    @property
    def input_total(self) -> int:
        with self._lock:
            return self._input_total

    @property
    def total(self) -> int:
        with self._lock:
            return self._output_total + self._input_total

    def record_usage(self, output_tokens: int, input_tokens: int = 0) -> None:
        """Record token usage after a call completes."""
        if output_tokens < 0 or input_tokens < 0:
            raise ValueError(
                f"record_usage: tokens must be non-negative, "
                f"got output={output_tokens}, input={input_tokens}"
            )
        with self._lock:
            self._output_total += output_tokens
            self._input_total += input_tokens

    def before_llm_call(self, ctx: ToolCallContext) -> Decision | None:
        """Check token budget before allowing next call."""
        with self._lock:
            # Check output budget
            if self._output_total >= self._max_output_tokens:
                return Decision.HALT

            degrade_at_output = self._degrade_threshold * self._max_output_tokens
            output_degraded = self._output_total >= degrade_at_output

            # Check total budget (if enabled)
            total_degraded = False
            if self._max_total_tokens > 0:
                total = self._output_total + self._input_total
                if total >= self._max_total_tokens:
                    return Decision.HALT
                degrade_at_total = self._degrade_threshold * self._max_total_tokens
                total_degraded = total >= degrade_at_total

            if output_degraded or total_degraded:
                return Decision.DEGRADE

            return None
