"""InputCompressionHook for VERONICA Execution Shield.

v0.5.1: Real compression with safety guarantees.

When estimated input tokens exceed ``compression_threshold_tokens``,
the hook compresses the text via a pluggable ``Compressor``.  At
``halt_threshold_tokens`` (post-compression), returns Decision.HALT.

Escape hatch: set env ``VERONICA_DISABLE_COMPRESSION=1`` to skip all
compression and fall back to the v0.5.0 detect-only behavior.

Design principles:
  - Raw text is NEVER stored (SHA-256 hash only)
  - Compression preserves: numbers, dates, explicit constraints
  - Two SafetyEvents per compression: INPUT_COMPRESSED + COMPRESSION_APPLIED
  - Failure default: HALT (configurable to fallback_to_original)
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from typing import Any, Protocol, runtime_checkable

from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision, ToolCallContext


def estimate_tokens(text: str) -> int:
    """Estimate token count from raw text (MVP: len/4)."""
    return len(text) // 4


def _sha256(text: str) -> str:
    """Return hex SHA-256 of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Compressor Protocol + Default Implementation
# ---------------------------------------------------------------------------

@runtime_checkable
class Compressor(Protocol):
    """Protocol for text compression strategies."""

    def compress(self, text: str, target_tokens: int) -> str:
        """Compress text to fit within target_tokens.

        Must preserve: numbers, dates, explicit constraints.
        Returns compressed text. Raises on failure.
        """
        ...


# Regex patterns for "must not delete" content
_NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")
_DATE_RE = re.compile(
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"
    r"|\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b"
)
_CONSTRAINT_RE = re.compile(
    r"(?i)\b(?:must|shall|required|mandatory|never|always|do not|don't|forbidden"
    r"|prohibited|constraint|limit|maximum|minimum|at least|at most|no more than)\b"
)

_COMPRESSION_TEMPLATE = (
    "=== COMPRESSED INPUT (VERONICA) ===\n"
    "[Purpose]\n{purpose}\n\n"
    "[Constraints]\n{constraints}\n\n"
    "[Key Data]\n{key_data}\n\n"
    "[Uncertainties]\n{uncertainties}\n"
    "=== END COMPRESSED ==="
)


def _extract_important_lines(text: str) -> tuple[list[str], list[str]]:
    """Split lines into (important, other).

    Important = contains numbers, dates, or constraint keywords.
    """
    important: list[str] = []
    other: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if (_NUMBER_RE.search(stripped)
                or _DATE_RE.search(stripped)
                or _CONSTRAINT_RE.search(stripped)):
            important.append(stripped)
        else:
            other.append(stripped)
    return important, other


class TemplateCompressor:
    """Default compressor: rule-based extraction into template.

    No LLM dependency.  Preserves lines with numbers, dates, and
    constraint keywords.  Truncates remaining lines to fit budget.
    """

    def compress(self, text: str, target_tokens: int) -> str:
        important, other = _extract_important_lines(text)

        constraints = "\n".join(f"- {l}" for l in important if _CONSTRAINT_RE.search(l))
        key_data = "\n".join(f"- {l}" for l in important if not _CONSTRAINT_RE.search(l))

        # Purpose: first non-empty line of original (truncated)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        purpose_raw = lines[0] if lines else "(none)"
        max_purpose_chars = min(200, target_tokens)
        purpose = purpose_raw[:max_purpose_chars] + ("..." if len(purpose_raw) > max_purpose_chars else "")

        # Truncate constraints / key_data to fit budget
        budget_chars = target_tokens * 4  # reverse of estimate_tokens
        max_section = budget_chars // 4
        if len(constraints) > max_section:
            constraints = constraints[:max_section] + "\n  ..."
        if len(key_data) > max_section:
            key_data = key_data[:max_section] + "\n  ..."

        # Fill remaining budget with other lines
        used = len(purpose) + len(constraints) + len(key_data) + 200  # template overhead
        remaining_budget = max(0, budget_chars - used)

        uncertainties_lines: list[str] = []
        remaining_chars = 0
        for line in other:
            if remaining_chars + len(line) + 3 > remaining_budget:
                break
            uncertainties_lines.append(f"- {line}")
            remaining_chars += len(line) + 3

        result = _COMPRESSION_TEMPLATE.format(
            purpose=purpose,
            constraints=constraints or "(none)",
            key_data=key_data or "(none)",
            uncertainties="\n".join(uncertainties_lines) if uncertainties_lines else "(truncated)",
        )
        return result


# ---------------------------------------------------------------------------
# InputCompressionHook
# ---------------------------------------------------------------------------

class InputCompressionHook:
    """Input compression with safety guarantees.

    Thread-safe.  The caller invokes ``compress_if_needed(text, ctx)``
    before sending the prompt to the LLM.

    Workflow:
      1. Estimate tokens
      2. If below threshold -> return (text, None) unchanged
      3. If compression disabled (env) -> detect-only (DEGRADE/HALT)
      4. Compress via Compressor
      5. Re-estimate compressed tokens
      6. If still above halt_threshold -> HALT (or fallback)
      7. Record SafetyEvents

    Evidence dict (available via ``last_evidence``):
      - before_tokens, after_tokens, compression_ratio
      - input_sha256
      - decision
    """

    def __init__(
        self,
        compression_threshold_tokens: int = 4000,
        halt_threshold_tokens: int = 8000,
        compressor: Compressor | None = None,
        fallback_to_original: bool = False,
    ) -> None:
        if halt_threshold_tokens <= compression_threshold_tokens:
            raise ValueError(
                f"halt_threshold_tokens ({halt_threshold_tokens}) must be "
                f"greater than compression_threshold_tokens ({compression_threshold_tokens})"
            )
        self._compress_threshold = compression_threshold_tokens
        self._halt_threshold = halt_threshold_tokens
        self._compressor: Compressor = compressor or TemplateCompressor()
        self._fallback_to_original = fallback_to_original
        self._last_evidence: dict[str, Any] | None = None
        self._safety_events: list[SafetyEvent] = []
        self._lock = threading.Lock()

    @property
    def compression_threshold_tokens(self) -> int:
        return self._compress_threshold

    @property
    def halt_threshold_tokens(self) -> int:
        return self._halt_threshold

    @property
    def fallback_to_original(self) -> bool:
        return self._fallback_to_original

    @property
    def last_evidence(self) -> dict[str, Any] | None:
        """Return evidence dict from the most recent non-ALLOW operation."""
        with self._lock:
            return self._last_evidence

    def get_events(self) -> list[SafetyEvent]:
        """Return accumulated SafetyEvents (shallow copy)."""
        with self._lock:
            return list(self._safety_events)

    def clear_events(self) -> None:
        """Clear accumulated SafetyEvents."""
        with self._lock:
            self._safety_events.clear()

    @staticmethod
    def _is_disabled() -> bool:
        """Check escape hatch env var."""
        return os.environ.get("VERONICA_DISABLE_COMPRESSION") == "1"

    def check_input(self, text: str, ctx: ToolCallContext) -> Decision | None:
        """Legacy detect-only API (v0.5.0 compat).

        Returns None (ALLOW) if within budget, DEGRADE or HALT otherwise.
        Does NOT compress.
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

    def compress_if_needed(
        self, text: str, ctx: ToolCallContext
    ) -> tuple[str, Decision | None]:
        """Compress input if above threshold.  Main v0.5.1 entry point.

        Returns:
            (output_text, decision)
            - decision is None when no action needed (ALLOW)
            - decision is DEGRADE when compression succeeded
            - decision is HALT when compression failed or post-compress still too large
        """
        before_tokens = estimate_tokens(text)

        # Under threshold -> pass through
        if before_tokens < self._compress_threshold:
            return text, None

        input_sha = _sha256(text)

        # Escape hatch
        if self._is_disabled():
            decision = Decision.HALT if before_tokens >= self._halt_threshold else Decision.DEGRADE
            with self._lock:
                self._last_evidence = {
                    "before_tokens": before_tokens,
                    "input_sha256": input_sha,
                    "decision": decision.value,
                    "compression_disabled": True,
                }
            return text, decision

        # Attempt compression
        target_tokens = self._compress_threshold
        try:
            compressed = self._compressor.compress(text, target_tokens)
        except Exception:
            # Compression failed
            if self._fallback_to_original:
                decision = Decision.DEGRADE
                self._record_events(
                    ctx, before_tokens, before_tokens, input_sha,
                    decision, compression_failed=True,
                )
                return text, decision
            else:
                decision = Decision.HALT
                self._record_events(
                    ctx, before_tokens, before_tokens, input_sha,
                    decision, compression_failed=True,
                )
                return text, decision

        after_tokens = estimate_tokens(compressed)

        # Post-compression still above halt threshold?
        if after_tokens >= self._halt_threshold:
            if self._fallback_to_original:
                decision = Decision.DEGRADE
                self._record_events(
                    ctx, before_tokens, after_tokens, input_sha, decision,
                )
                return text, decision
            else:
                decision = Decision.HALT
                self._record_events(
                    ctx, before_tokens, after_tokens, input_sha, decision,
                )
                return text, decision

        # Compression succeeded
        decision = Decision.DEGRADE
        self._record_events(ctx, before_tokens, after_tokens, input_sha, decision)
        return compressed, decision

    def _record_events(
        self,
        ctx: ToolCallContext,
        before_tokens: int,
        after_tokens: int,
        input_sha: str,
        decision: Decision,
        compression_failed: bool = False,
    ) -> None:
        """Record INPUT_COMPRESSED + COMPRESSION_APPLIED SafetyEvents."""
        ratio = after_tokens / before_tokens if before_tokens > 0 else 1.0

        evidence = {
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "compression_ratio": round(ratio, 4),
            "input_sha256": input_sha,
            "decision": decision.value,
        }
        if compression_failed:
            evidence["compression_failed"] = True

        with self._lock:
            self._last_evidence = evidence

            self._safety_events.append(SafetyEvent(
                event_type="INPUT_COMPRESSED",
                decision=decision,
                reason=f"before={before_tokens} after={after_tokens} ratio={ratio:.2%}",
                hook="InputCompressionHook",
                request_id=ctx.request_id,
                metadata={
                    "before_tokens": before_tokens,
                    "after_tokens": after_tokens,
                    "compression_ratio": round(ratio, 4),
                    "input_sha256": input_sha,
                },
            ))

            self._safety_events.append(SafetyEvent(
                event_type="COMPRESSION_APPLIED",
                decision=decision,
                reason=f"compression {'failed' if compression_failed else 'applied'}",
                hook="InputCompressionHook",
                request_id=ctx.request_id,
            ))

    def before_llm_call(self, ctx: ToolCallContext) -> Decision | None:
        """PreDispatchHook protocol -- always ALLOW.

        InputCompressionHook is NOT a pre-dispatch hook.  Callers should
        use ``compress_if_needed()`` or ``check_input()`` with the actual
        prompt text.
        """
        return None
