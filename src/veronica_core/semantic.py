"""veronica_core.semantic — Semantic loop detection guard.

Detects when an LLM produces semantically repetitive outputs by computing
pairwise Jaccard similarity over a rolling window of recent outputs.

No heavy dependencies — pure Python only.

Public API:
    SemanticLoopGuard — RuntimePolicy that detects semantic loops
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, FrozenSet, Tuple

from veronica_core.runtime_policy import PolicyContext, PolicyDecision

__all__ = ["SemanticLoopGuard"]


@dataclass
class SemanticLoopGuard:
    """Detects semantic loop patterns in LLM output via Jaccard similarity.

    Maintains a rolling window of recent outputs. On each check, compares
    all pairs using word-level Jaccard similarity. If any pair exceeds the
    threshold, denies further execution.

    Args:
        window: Number of recent outputs to retain for comparison.
        jaccard_threshold: Similarity threshold [0, 1] above which two outputs
            are considered semantically looping.
        min_chars: Minimum character length (after normalization) to consider
            for comparison. Short outputs are skipped to avoid false positives.

    Example::

        guard = SemanticLoopGuard(window=3, jaccard_threshold=0.92)
        container = AIContainer(semantic_guard=guard)
        guard.feed("The answer is 42.")
        guard.feed("The answer is 42.")  # -> deny
    """

    window: int = 3
    jaccard_threshold: float = 0.92
    min_chars: int = 80
    policy_type: str = field(default="semantic_loop", init=False)

    # Buffer stores (normalized_str, frozenset_of_words) tuples
    _buffer: Deque[Tuple[str, FrozenSet[str]]] = field(init=False)

    def __post_init__(self) -> None:
        self._buffer = deque(maxlen=self.window)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(text: str) -> str:
        """Lowercase, strip, and collapse internal whitespace."""
        return re.sub(r"\s+", " ", text.strip().lower())

    @staticmethod
    def _tokenize(normalized: str) -> FrozenSet[str]:
        """Split normalized text into a frozen word set."""
        return frozenset(normalized.split())

    @staticmethod
    def _jaccard(a: FrozenSet[str], b: FrozenSet[str]) -> float:
        """Jaccard similarity between two word sets. Returns 0.0 for empty sets."""
        if not a and not b:
            return 1.0  # both empty -> identical
        union = a | b
        if not union:
            return 0.0
        return len(a & b) / len(union)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, text: str) -> None:
        """Append *text* to the rolling buffer without checking."""
        norm = self._normalize(text)
        tokens = self._tokenize(norm)
        self._buffer.append((norm, tokens))

    def check(self, context: PolicyContext | None = None) -> PolicyDecision:
        """Check the current buffer for semantic loops.

        Returns:
            PolicyDecision(allowed=True) if no loop detected.
            PolicyDecision(allowed=False, reason="semantic_loop") if loop found.
        """
        if context is None:
            context = PolicyContext()

        entries = list(self._buffer)
        n = len(entries)

        for i in range(n):
            norm_i, tokens_i = entries[i]
            # Skip short outputs
            if len(norm_i) < self.min_chars:
                continue
            for j in range(i + 1, n):
                norm_j, tokens_j = entries[j]
                if len(norm_j) < self.min_chars:
                    continue
                # Exact-match shortcut
                if norm_i == norm_j:
                    return PolicyDecision(
                        allowed=False,
                        policy_type=self.policy_type,
                        reason=(
                            f"semantic_loop: exact repetition detected "
                            f"(entry {i} == entry {j})"
                        ),
                    )
                # Jaccard similarity check
                sim = self._jaccard(tokens_i, tokens_j)
                if sim >= self.jaccard_threshold:
                    return PolicyDecision(
                        allowed=False,
                        policy_type=self.policy_type,
                        reason=(
                            f"semantic_loop: Jaccard similarity {sim:.3f} >= "
                            f"{self.jaccard_threshold} (entries {i} and {j})"
                        ),
                    )

        return PolicyDecision(allowed=True, policy_type=self.policy_type)

    def feed(self, text: str) -> PolicyDecision:
        """Record *text* and immediately check for loops.

        Convenience method combining record() + check().

        Returns:
            PolicyDecision from check() after recording.
        """
        self.record(text)
        return self.check()

    def reset(self) -> None:
        """Clear the rolling buffer."""
        self._buffer.clear()
