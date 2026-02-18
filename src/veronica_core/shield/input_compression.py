"""InputCompressionHook for VERONICA Execution Shield.

Skeleton implementation: decision + evidence only, NO actual compression.
When estimated input tokens exceed ``compression_threshold_tokens``,
returns Decision.DEGRADE.  At ``halt_threshold_tokens``, returns Decision.HALT.

Token estimation is MVP: ``len(text) / 4``.  The hook records an
``INPUT_TOO_LARGE`` SafetyEvent with ``input_sha256`` for audit -- raw
text is never stored.

Actual compression logic is deferred to v0.5.1+.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any

from veronica_core.shield.types import Decision, ToolCallContext


def estimate_tokens(text: str) -> int:
    """Estimate token count from raw text (MVP: len/4)."""
    return len(text) // 4


def _sha256(text: str) -> str:
    """Return hex SHA-256 of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class InputCompressionHook:
    """Input size gate with optional DEGRADE zone.

    Thread-safe.  The caller invokes ``check_input(text, ctx)`` before
    sending the prompt to the LLM.  The hook does NOT modify the text --
    it only returns a Decision and records evidence.

    Decision logic:
      - estimated_tokens >= halt_threshold  -> HALT
      - estimated_tokens >= compress_threshold -> DEGRADE
      - otherwise -> None (ALLOW)

    Evidence dict (available via ``last_evidence``):
      - estimated_tokens: int
      - input_sha256: str
      - decision: str
    """

    def __init__(
        self,
        compression_threshold_tokens: int = 4000,
        halt_threshold_tokens: int = 8000,
    ) -> None:
        if halt_threshold_tokens <= compression_threshold_tokens:
            raise ValueError(
                f"halt_threshold_tokens ({halt_threshold_tokens}) must be "
                f"greater than compression_threshold_tokens ({compression_threshold_tokens})"
            )
        self._compress_threshold = compression_threshold_tokens
        self._halt_threshold = halt_threshold_tokens
        self._last_evidence: dict[str, Any] | None = None
        self._lock = threading.Lock()

    @property
    def compression_threshold_tokens(self) -> int:
        return self._compress_threshold

    @property
    def halt_threshold_tokens(self) -> int:
        return self._halt_threshold

    @property
    def last_evidence(self) -> dict[str, Any] | None:
        """Return evidence dict from the most recent non-ALLOW check."""
        with self._lock:
            return self._last_evidence

    def check_input(self, text: str, ctx: ToolCallContext) -> Decision | None:
        """Check input size and return a Decision.

        Returns None (ALLOW) if within budget, DEGRADE or HALT otherwise.
        """
        tokens = estimate_tokens(text)

        if tokens < self._compress_threshold:
            return None

        sha = _sha256(text)

        if tokens >= self._halt_threshold:
            decision = Decision.HALT
        else:
            decision = Decision.DEGRADE

        evidence = {
            "estimated_tokens": tokens,
            "input_sha256": sha,
            "decision": decision.value,
            "compression_threshold": self._compress_threshold,
            "halt_threshold": self._halt_threshold,
        }

        with self._lock:
            self._last_evidence = evidence

        return decision

    def before_llm_call(self, ctx: ToolCallContext) -> Decision | None:
        """PreDispatchHook protocol -- always ALLOW.

        InputCompressionHook is NOT a pre-dispatch hook.  Callers should
        use ``check_input()`` with the actual prompt text.  This method
        exists only so the hook can be wired into the pipeline for future
        use; it always returns None (ALLOW).
        """
        return None
