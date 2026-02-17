"""VERONICA Partial Result Preservation - Captures partial output on interruption.

Note: This module replaces the legacy VeronicaPersistence partial-save logic
with a dedicated streaming buffer for LLM output preservation.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Any, Dict
import logging

logger = logging.getLogger(__name__)


@dataclass
class PartialResultBuffer:
    """Captures and preserves partial results during LLM streaming or multi-step execution.

    When an LLM call is interrupted (timeout, abort, budget exceeded),
    accumulated chunks are preserved instead of silently discarded.

    Example:
        buf = PartialResultBuffer()
        for chunk in llm_stream:
            buf.append(chunk)
        buf.mark_complete()
        # On interruption: buf.get_partial() returns accumulated text
    """

    _chunks: List[str] = field(default_factory=list, init=False)
    _metadata: Dict[str, Any] = field(default_factory=dict, init=False)
    _is_complete: bool = field(default=False, init=False)

    def append(self, chunk: str) -> None:
        """Append a streaming chunk.

        Args:
            chunk: Text chunk from LLM stream
        """
        self._chunks.append(chunk)

    def mark_complete(self) -> None:
        """Mark the result as complete (not partial)."""
        self._is_complete = True

    def get_partial(self) -> str:
        """Get accumulated partial result as a single string."""
        return "".join(self._chunks)

    @property
    def chunk_count(self) -> int:
        """Number of chunks accumulated."""
        return len(self._chunks)

    @property
    def is_complete(self) -> bool:
        """True if result was fully received."""
        return self._is_complete

    @property
    def is_partial(self) -> bool:
        """True if chunks exist but result is incomplete."""
        return len(self._chunks) > 0 and not self._is_complete

    def set_metadata(self, key: str, value: Any) -> None:
        """Attach metadata (e.g., model, token count, abort reason).

        Args:
            key: Metadata key
            value: Metadata value
        """
        self._metadata[key] = value

    @property
    def metadata(self) -> Dict[str, Any]:
        """Get copy of metadata dict."""
        return dict(self._metadata)

    def clear(self) -> None:
        """Clear buffer for reuse."""
        self._chunks.clear()
        self._metadata.clear()
        self._is_complete = False

    def to_dict(self) -> Dict:
        """Serialize buffer state."""
        return {
            "partial_text": self.get_partial(),
            "chunk_count": self.chunk_count,
            "is_complete": self._is_complete,
            "metadata": dict(self._metadata),
        }
